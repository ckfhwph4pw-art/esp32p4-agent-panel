/*
 * 智能体副屏 V2 —— 四页横向滑动
 *   第 1 页：本机（处理器 / 内存 / 磁盘 / 负载，横向条形图）
 *   第 2~4 页：WorkBuddy / Hermes / DeepSeek
 *
 * 数据通道：UART（CH343）与 USB CDC（OTG 口）双通道，行分隔 JSON。
 *
 * V2 六项改动：
 *   1. 顶栏与底栏英文字号统一降到 Orbitron 20，消除溢出与遮挡
 *   2. 底栏提示改用 16 号中文，不再与正文同大小
 *   3. 页码改为嵌套圆环 + 数字
 *   4. 标识/数据源/刷新 三行压成一行三列，标签在上数值在下，字号降到 16
 *   5. 新增本机硬件页并置于首位
 *   6. 状态灯改为圆形品牌徽章（外环表状态、内圆为品牌色 + 字母）
 */

#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_err.h"
#include "cJSON.h"
#include "bsp/esp-bsp.h"
#include "lvgl.h"
#include "tinyusb.h"
#include "tinyusb_cdc_acm.h"
#include "tinyusb_default_config.h"

extern const lv_font_t lv_font_noto30;
extern const lv_font_t lv_font_noto16;
extern const lv_font_t lv_font_orbitron48;
extern const lv_font_t lv_font_orbitron32;
extern const lv_font_t lv_font_orbitron20;

#define C_BG     lv_color_hex(0x0B0F14)
#define C_BAR    lv_color_hex(0x131A22)
#define C_TXT    lv_color_hex(0xE6EDF3)
#define C_SUB    lv_color_hex(0x8B98A5)
#define C_DIM    lv_color_hex(0x4B5563)
#define C_RUN    lv_color_hex(0x3B82F6)
#define C_WAIT   lv_color_hex(0xF59E0B)
#define C_FAIL   lv_color_hex(0xEF4444)
#define C_IDLE   lv_color_hex(0x22C55E)
#define C_CMD    lv_color_hex(0x7DD3FC)
#define C_TRACK  lv_color_hex(0x1F2937)

typedef enum { ST_UNKNOWN, ST_BUSY, ST_IDLE, ST_WAIT, ST_ERR, ST_OFF } st_t;

typedef struct {
    const char *id;
    const char *name;
    const char *badge;
    uint32_t    brand;
    st_t        st;
    char        task[96];
    char        ago[20];
    int         elapsed;
    time_t      last_rx;
    char        cmd[56];
    int         cnt;
    char        csub[24];
    char        heat[25];
    int         cpu, mem, disk, load10;
} agent_t;

static agent_t s_agent[4] = {
    { "pc",        "本机",      "P", 0x22D3EE, ST_UNKNOWN, "等待数据", "--", -1, 0, "", -1, "", "", 0, 0, 0, 0 },
    { "workbuddy", "WorkBuddy", "W", 0x0BC894, ST_UNKNOWN, "等待数据", "--", -1, 0, "", -1, "", "", 0, 0, 0, 0 },
    { "hermes",    "Hermes",    "H", 0xF59E0B, ST_UNKNOWN, "等待数据", "--", -1, 0, "", -1, "", "", 0, 0, 0, 0 },
    { "deepseek",  "DeepSeek",  "D", 0x4D6BFE, ST_UNKNOWN, "等待数据", "--", -1, 0, "", -1, "", "", 0, 0, 0, 0 },
};

typedef struct {
    lv_obj_t *ring;
    lv_obj_t *badge;
    lv_obj_t *badge_txt;
    lv_obj_t *word;
    lv_obj_t *sub;
    lv_obj_t *task;
    lv_obj_t *cmd;
    lv_obj_t *cap;
    lv_obj_t *big;
    lv_obj_t *cnt_num;
    lv_obj_t *cnt_cap;
    lv_obj_t *heat[24];
    lv_obj_t *lbl[3];
    lv_obj_t *val[3];
    lv_obj_t *bar[3];
    lv_obj_t *bval[3];
    lv_obj_t *foot1;
    lv_obj_t *foot2;
} page_ui_t;

static page_ui_t s_ui[4];
static lv_obj_t *s_tv;
static lv_obj_t *s_num[4];
static lv_obj_t *s_ring[4];
static lv_obj_t *s_link;
static lv_obj_t *s_mini[4];

static void f_text(lv_obj_t *o)  { lv_obj_set_style_text_font(o, &lv_font_noto30, 0); }
static void f_small(lv_obj_t *o) { lv_obj_set_style_text_font(o, &lv_font_noto16, 0); }
static void f_num(lv_obj_t *o)   { lv_obj_set_style_text_font(o, &lv_font_orbitron20, 0); }

static lv_color_t state_color(st_t st)
{
    switch (st) {
        case ST_BUSY: return C_RUN;
        case ST_IDLE: return C_IDLE;
        case ST_WAIT: return C_WAIT;
        case ST_ERR:  return C_FAIL;
        case ST_OFF:  return C_DIM;
        default:      return C_SUB;
    }
}

static const char *state_word(st_t st)
{
    switch (st) {
        case ST_BUSY: return "工作中";
        case ST_IDLE: return "空闲";
        case ST_WAIT: return "等你决定";
        case ST_ERR:  return "出错";
        case ST_OFF:  return "已离线";
        default:      return "未知";
    }
}

static void opa_anim(void *var, int32_t v)
{
    lv_obj_set_style_opa((lv_obj_t *)var, (lv_opa_t)v, 0);
}

static void pulse_start(lv_obj_t *o)
{
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, o);
    lv_anim_set_values(&a, LV_OPA_COVER, LV_OPA_30);
    lv_anim_set_time(&a, 1200);
    lv_anim_set_playback_time(&a, 1200);
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_exec_cb(&a, opa_anim);
    lv_anim_start(&a);
}

static void pulse_stop(lv_obj_t *o)
{
    lv_anim_del(o, opa_anim);
    lv_obj_set_style_opa(o, LV_OPA_COVER, 0);
}

static lv_obj_t *plain_box(lv_obj_t *parent, int w, int h, lv_color_t color, int radius)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_set_size(o, w, h);
    lv_obj_set_style_bg_color(o, color, 0);
    lv_obj_set_style_border_width(o, 0, 0);
    lv_obj_set_style_radius(o, radius, 0);
    lv_obj_set_style_pad_all(o, 0, 0);
    lv_obj_clear_flag(o, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_clear_flag(o, LV_OBJ_FLAG_CLICKABLE);
    return o;
}

/* 四页各自的官方图标（白色透明底，38x38，ARGB8888） */
extern const lv_image_dsc_t img_apple;
extern const lv_image_dsc_t img_workbuddy;
extern const lv_image_dsc_t img_hermes;
extern const lv_image_dsc_t img_deepseek;
extern const lv_image_dsc_t img_apple_mini;
extern const lv_image_dsc_t img_workbuddy_mini;
extern const lv_image_dsc_t img_hermes_mini;
extern const lv_image_dsc_t img_deepseek_mini;

static const lv_image_dsc_t *page_icon(int idx)
{
    switch (idx) {
    case 0:  return &img_apple;
    case 1:  return &img_workbuddy;
    case 2:  return &img_hermes;
    default: return &img_deepseek;
    }
}

static const lv_image_dsc_t *mini_icon(int idx)
{
    switch (idx) {
    case 0:  return &img_apple_mini;
    case 1:  return &img_workbuddy_mini;
    case 2:  return &img_hermes_mini;
    default: return &img_deepseek_mini;
    }
}

/* 品牌徽章：外环状态色，内圆品牌色，中央官方图标 */
static void make_badge(lv_obj_t *parent, int idx, lv_obj_t **ring, lv_obj_t **inner, lv_obj_t **txt)
{
    agent_t *a = &s_agent[idx];

    lv_obj_t *r = lv_obj_create(parent);
    lv_obj_set_size(r, 72, 72);
    lv_obj_set_style_radius(r, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(r, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(r, 3, 0);
    lv_obj_set_style_border_color(r, state_color(a->st), 0);
    lv_obj_set_style_pad_all(r, 0, 0);
    lv_obj_clear_flag(r, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_clear_flag(r, LV_OBJ_FLAG_CLICKABLE);
    *ring = r;

    lv_obj_t *in = plain_box(r, 60, 60, lv_color_hex(a->brand), LV_RADIUS_CIRCLE);
    lv_obj_center(in);
    *inner = in;

    lv_obj_t *img = lv_image_create(in);
    lv_image_set_src(img, page_icon(idx));
    lv_obj_center(img);
    lv_obj_clear_flag(img, LV_OBJ_FLAG_CLICKABLE);
    *txt = img;
}

static void build_agent_page(lv_obj_t *tile, int idx)
{
    page_ui_t *u = &s_ui[idx];
    agent_t *a = &s_agent[idx];

    lv_obj_set_style_bg_color(tile, C_BG, 0);
    lv_obj_set_style_pad_all(tile, 0, 0);
    lv_obj_clear_flag(tile, LV_OBJ_FLAG_SCROLLABLE);

    make_badge(tile, idx, &u->ring, &u->badge, &u->badge_txt);
    lv_obj_set_pos(u->ring, 24, 66);

    u->word = lv_label_create(tile);
    lv_label_set_text(u->word, state_word(a->st));
    f_text(u->word);
    lv_obj_set_style_text_color(u->word, lv_color_hex(a->brand), 0);
    lv_obj_set_style_text_letter_space(u->word, 2, 0);
    lv_obj_set_pos(u->word, 112, 68);

    u->sub = lv_label_create(tile);
    lv_label_set_text(u->sub, "最后活动 --");
    f_small(u->sub);
    lv_obj_set_style_text_color(u->sub, C_SUB, 0);
    lv_obj_set_pos(u->sub, 112, 110);

    u->task = lv_label_create(tile);
    lv_label_set_text(u->task, "等待数据");
    f_text(u->task);
    lv_obj_set_style_text_color(u->task, C_TXT, 0);
    lv_obj_set_width(u->task, 432);
    lv_label_set_long_mode(u->task, LV_LABEL_LONG_DOT);
    lv_obj_set_pos(u->task, 24, 168);

    u->cmd = lv_label_create(tile);
    lv_label_set_text(u->cmd, "");
    f_small(u->cmd);
    lv_obj_set_style_text_color(u->cmd, C_CMD, 0);
    lv_obj_set_width(u->cmd, 432);
    lv_label_set_long_mode(u->cmd, LV_LABEL_LONG_DOT);
    lv_obj_set_pos(u->cmd, 24, 212);

    u->cap = lv_label_create(tile);
    lv_label_set_text(u->cap, "本轮耗时");
    f_small(u->cap);
    lv_obj_set_style_text_color(u->cap, C_SUB, 0);
    lv_obj_set_pos(u->cap, 24, 296);

    u->big = lv_label_create(tile);
    lv_label_set_text(u->big, "0:00");
    lv_obj_set_style_text_font(u->big, &lv_font_orbitron48, 0);
    lv_obj_set_style_text_color(u->big, C_TXT, 0);
    lv_obj_set_pos(u->big, 24, 320);
    lv_obj_add_flag(u->big, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(u->cap, LV_OBJ_FLAG_HIDDEN);

    u->cnt_cap = lv_label_create(tile);
    lv_label_set_text(u->cnt_cap, "");
    f_small(u->cnt_cap);
    lv_obj_set_style_text_color(u->cnt_cap, C_SUB, 0);
    lv_obj_set_pos(u->cnt_cap, 284, 296);
    lv_obj_add_flag(u->cnt_cap, LV_OBJ_FLAG_HIDDEN);

    u->cnt_num = lv_label_create(tile);
    lv_label_set_text(u->cnt_num, "--");
    lv_obj_set_style_text_font(u->cnt_num, &lv_font_orbitron48, 0);
    lv_obj_set_style_text_color(u->cnt_num, C_TXT, 0);
    lv_obj_set_pos(u->cnt_num, 284, 324);
    lv_obj_add_flag(u->cnt_num, LV_OBJ_FLAG_HIDDEN);

    for (int k = 0; k < 24; k++) {
        lv_obj_t *hb = plain_box(tile, 12, 3, C_DIM, 1);
        lv_obj_set_pos(hb, 24 + k * 18, 475);
        lv_obj_set_style_bg_opa(hb, LV_OPA_40, 0);
        u->heat[k] = hb;
    }

    lv_obj_t *sep = plain_box(tile, 432, 1, C_TRACK, 0);
    lv_obj_set_pos(sep, 24, 486);

    static const char *names[3] = { "标识", "数据源", "刷新" };
    for (int k = 0; k < 3; k++) {
        lv_obj_t *l = lv_label_create(tile);
        lv_label_set_text(l, names[k]);
        f_small(l);
        lv_obj_set_style_text_color(l, C_DIM, 0);
        lv_obj_set_pos(l, 24 + k * 152, 496);
        u->lbl[k] = l;

        lv_obj_t *v = lv_label_create(tile);
        lv_label_set_text(v, "--");
        f_small(v);
        lv_obj_set_style_text_color(v, C_TXT, 0);
        lv_obj_set_width(v, 140);
        lv_label_set_long_mode(v, LV_LABEL_LONG_DOT);
        lv_obj_set_pos(v, 24 + k * 152, 522);
        u->val[k] = v;
    }
}

static void build_pc_page(lv_obj_t *tile)
{
    page_ui_t *u = &s_ui[0];
    agent_t *a = &s_agent[0];

    lv_obj_set_style_bg_color(tile, C_BG, 0);
    lv_obj_set_style_pad_all(tile, 0, 0);
    lv_obj_clear_flag(tile, LV_OBJ_FLAG_SCROLLABLE);

    make_badge(tile, 0, &u->ring, &u->badge, &u->badge_txt);
    lv_obj_set_pos(u->ring, 24, 40);

    u->word = lv_label_create(tile);
    lv_label_set_text(u->word, "本机");
    f_text(u->word);
    lv_obj_set_style_text_color(u->word, lv_color_hex(a->brand), 0);
    lv_obj_set_pos(u->word, 112, 44);

    u->sub = lv_label_create(tile);
    lv_label_set_text(u->sub, "等待数据");
    f_small(u->sub);
    lv_obj_set_style_text_color(u->sub, C_SUB, 0);
    lv_obj_set_width(u->sub, 340);
    lv_label_set_long_mode(u->sub, LV_LABEL_LONG_DOT);
    lv_obj_set_pos(u->sub, 112, 86);

    static const char *names[3] = { "处理器", "内存", "磁盘" };
    for (int k = 0; k < 3; k++) {
        int y = 168 + k * 96;

        lv_obj_t *l = lv_label_create(tile);
        lv_label_set_text(l, names[k]);
        f_text(l);
        lv_obj_set_style_text_color(l, C_SUB, 0);
        lv_obj_set_pos(l, 24, y);

        lv_obj_t *bar = lv_bar_create(tile);
        lv_obj_set_size(bar, 300, 16);
        lv_obj_set_pos(bar, 24, y + 44);
        lv_bar_set_range(bar, 0, 100);
        lv_bar_set_value(bar, 0, LV_ANIM_OFF);
        lv_obj_set_style_bg_color(bar, C_TRACK, LV_PART_MAIN);
        lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, LV_PART_MAIN);
        lv_obj_set_style_radius(bar, 8, LV_PART_MAIN);
        lv_obj_set_style_bg_color(bar, lv_color_hex(a->brand), LV_PART_INDICATOR);
        lv_obj_set_style_radius(bar, 8, LV_PART_INDICATOR);
        u->bar[k] = bar;

        lv_obj_t *v = lv_label_create(tile);
        lv_label_set_text(v, "--");
        lv_obj_set_style_text_font(v, &lv_font_orbitron32, 0);
        lv_obj_set_style_text_color(v, C_TXT, 0);
        lv_obj_align_to(v, bar, LV_ALIGN_OUT_RIGHT_MID, 16, 0);
        u->bval[k] = v;
    }

    lv_obj_t *sep = plain_box(tile, 432, 1, C_TRACK, 0);
    lv_obj_set_pos(sep, 24, 486);

    u->foot1 = lv_label_create(tile);
    lv_label_set_text(u->foot1, "负载 --");
    f_small(u->foot1);
    lv_obj_set_style_text_color(u->foot1, C_SUB, 0);
    lv_obj_set_pos(u->foot1, 24, 500);

    u->foot2 = lv_label_create(tile);
    lv_label_set_text(u->foot2, "核心 --");
    f_small(u->foot2);
    lv_obj_set_style_text_color(u->foot2, C_SUB, 0);
    lv_obj_set_pos(u->foot2, 240, 500);
}

static void update_pc_page(void)
{
    page_ui_t *u = &s_ui[0];
    agent_t *a = &s_agent[0];

    lv_obj_set_style_border_color(u->ring, state_color(a->st), 0);
    lv_label_set_text(u->sub, a->task);

    int vals[3] = { a->cpu, a->mem, a->disk };
    for (int k = 0; k < 3; k++) {
        int v = vals[k];
        if (v < 0) v = 0;
        if (v > 100) v = 100;
        lv_bar_set_value(u->bar[k], v, LV_ANIM_OFF);
        lv_label_set_text_fmt(u->bval[k], "%d%%", v);
    }

    lv_label_set_text_fmt(u->foot1, "负载 %.2f", a->load10 / 100.0);
    lv_label_set_text_fmt(u->foot2, "核心 %d", a->cnt);
}

static void update_agent_page(int i)
{
    page_ui_t *u = &s_ui[i];
    agent_t *a = &s_agent[i];

    lv_obj_set_style_border_color(u->ring, state_color(a->st), 0);
    if (a->st == ST_BUSY) pulse_start(u->ring);
    else                  pulse_stop(u->ring);

    lv_label_set_text(u->word, state_word(a->st));
    lv_obj_set_style_text_color(u->word, lv_color_hex(a->brand), 0);

    lv_label_set_text_fmt(u->sub, "最后活动 %s", a->ago);
    lv_label_set_text(u->task, a->task);
    lv_label_set_text(u->cmd, a->cmd);

    if (a->elapsed >= 0) {
        lv_label_set_text_fmt(u->big, "%d:%02d", a->elapsed / 60, a->elapsed % 60);
        lv_obj_clear_flag(u->big, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(u->cap, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(u->big, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(u->cap, LV_OBJ_FLAG_HIDDEN);
    }

    if (a->cnt >= 0) {
        lv_label_set_text_fmt(u->cnt_num, "%d", a->cnt);
        lv_obj_clear_flag(u->cnt_num, LV_OBJ_FLAG_HIDDEN);
        if (a->csub[0]) {
            lv_label_set_text(u->cnt_cap, a->csub);
            lv_obj_clear_flag(u->cnt_cap, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(u->cnt_cap, LV_OBJ_FLAG_HIDDEN);
        }
    } else {
        lv_obj_add_flag(u->cnt_num, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(u->cnt_cap, LV_OBJ_FLAG_HIDDEN);
    }

    for (int k = 0; k < 24; k++) {
        int v = a->heat[k] ? (a->heat[k] - '0') : 0;
        if (v < 0 || v > 9) v = 0;
        int hh = (v == 0) ? 3 : (4 + v * 2);
        lv_obj_set_size(u->heat[k], 12, hh);
        lv_obj_set_pos(u->heat[k], 24 + k * 18, 478 - hh);
        lv_obj_set_style_bg_color(u->heat[k], v ? lv_color_hex(a->brand) : C_DIM, 0);
        lv_obj_set_style_bg_opa(u->heat[k], v ? LV_OPA_COVER : LV_OPA_40, 0);
    }

    lv_label_set_text(u->val[0], a->id);
    lv_label_set_text(u->val[1], "USB CDC");
    if (a->last_rx) {
        struct tm tm;
        localtime_r(&a->last_rx, &tm);
        lv_label_set_text_fmt(u->val[2], "%02d:%02d:%02d", tm.tm_hour, tm.tm_min, tm.tm_sec);
    } else {
        lv_label_set_text(u->val[2], "尚未收到");
    }
}

static void update_page(int i)
{
    if (i == 0) update_pc_page();
    else        update_agent_page(i);
}

static void update_all_pages(void)
{
    for (int i = 0; i < 4; i++) update_page(i);
}

static void update_page_dots(void)
{
    lv_obj_t *tile = lv_tileview_get_tile_active(s_tv);
    int cur = tile ? (int)lv_obj_get_index(tile) : 0;

    for (int i = 0; i < 4; i++) {
        bool on = (i == cur);
        lv_obj_set_style_border_color(s_ring[i], on ? lv_color_hex(s_agent[i].brand) : C_DIM, 0);
        lv_obj_set_style_border_width(s_ring[i], on ? 2 : 1, 0);
        lv_obj_set_style_text_color(s_num[i], on ? lv_color_hex(s_agent[i].brand) : C_DIM, 0);
        lv_obj_set_style_bg_color(s_mini[i], lv_color_hex(s_agent[i].brand), 0);
        lv_obj_set_style_opa(s_mini[i], on ? LV_OPA_COVER : LV_OPA_40, 0);
    }
}

static void on_tile_changed(lv_event_t *e)
{
    (void)e;
    lv_obj_t *tile = lv_tileview_get_tile_active(s_tv);
    int idx = tile ? (int)lv_obj_get_index(tile) : 0;
    update_page_dots();
    printf("切到第 %d 页 %s\n", idx + 1, s_agent[idx].name);
}

static void build_ui(void)
{
    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, C_BG, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *bar = plain_box(scr, 480, 56, C_BAR, 0);
    lv_obj_set_pos(bar, 0, 0);

    for (int i = 0; i < 4; i++) {
        s_mini[i] = plain_box(bar, 22, 22, lv_color_hex(s_agent[i].brand), LV_RADIUS_CIRCLE);
        lv_obj_set_pos(s_mini[i], 14 + i * 30, 17);

        lv_obj_t *mi = lv_image_create(s_mini[i]);
        lv_image_set_src(mi, mini_icon(i));
        lv_obj_center(mi);
        lv_obj_clear_flag(mi, LV_OBJ_FLAG_CLICKABLE);
    }

    lv_obj_t *brand = lv_label_create(bar);
    lv_label_set_text(brand, "AGENT PANEL");
    f_num(brand);
    lv_obj_set_style_text_color(brand, C_SUB, 0);
    lv_obj_set_pos(brand, 142, 18);

    s_link = lv_label_create(bar);
    lv_label_set_text(s_link, "WAIT");
    f_num(s_link);
    lv_obj_set_style_text_color(s_link, C_SUB, 0);
    lv_obj_align(s_link, LV_ALIGN_RIGHT_MID, -16, 0);

    s_tv = lv_tileview_create(scr);
    lv_obj_set_size(s_tv, 480, 656);
    lv_obj_set_pos(s_tv, 0, 56);
    lv_obj_set_style_bg_color(s_tv, C_BG, 0);
    lv_obj_set_style_border_width(s_tv, 0, 0);
    lv_obj_set_style_radius(s_tv, 0, 0);
    lv_obj_set_style_pad_all(s_tv, 0, 0);
    lv_obj_set_scrollbar_mode(s_tv, LV_SCROLLBAR_MODE_OFF);

    for (int i = 0; i < 4; i++) {
        lv_obj_t *tile = lv_tileview_add_tile(s_tv, i, 0, LV_DIR_HOR);
        if (i == 0) build_pc_page(tile);
        else        build_agent_page(tile, i);
    }
    lv_obj_add_event_cb(s_tv, on_tile_changed, LV_EVENT_VALUE_CHANGED, NULL);

    lv_obj_t *foot = plain_box(scr, 480, 88, C_BAR, 0);
    lv_obj_set_pos(foot, 0, 712);

    lv_obj_t *row = lv_obj_create(foot);
    lv_obj_set_size(row, 480, 34);
    lv_obj_set_pos(row, 0, 8);
    lv_obj_set_style_bg_opa(row, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(row, 0, 0);
    lv_obj_set_style_pad_all(row, 0, 0);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(row, 12, 0);
    lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);

    for (int i = 0; i < 4; i++) {
        lv_obj_t *ring = lv_obj_create(row);
        lv_obj_set_size(ring, 30, 30);
        lv_obj_set_style_radius(ring, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_opa(ring, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(ring, 1, 0);
        lv_obj_set_style_border_color(ring, C_DIM, 0);
        lv_obj_set_style_pad_all(ring, 0, 0);
        lv_obj_clear_flag(ring, LV_OBJ_FLAG_SCROLLABLE);
        s_ring[i] = ring;

        lv_obj_t *n = lv_label_create(ring);
        lv_label_set_text_fmt(n, "%d", i + 1);
        lv_obj_set_style_text_font(n, &lv_font_orbitron20, 0);
        lv_obj_set_style_text_color(n, C_DIM, 0);
        lv_obj_center(n);
        s_num[i] = n;
    }

    lv_obj_t *hint = lv_label_create(foot);
    lv_label_set_text(hint, "左右滑动切换页面");
    f_small(hint);
    lv_obj_set_style_text_color(hint, C_DIM, 0);
    lv_obj_align(hint, LV_ALIGN_TOP_MID, 0, 48);
}

static int agent_index(const char *id)
{
    for (int i = 0; i < 4; i++) {
        if (strcmp(id, s_agent[i].id) == 0) return i;
    }
    return -1;
}

static st_t parse_state(const char *s)
{
    if (!s) return ST_UNKNOWN;
    if (strcmp(s, "busy") == 0) return ST_BUSY;
    if (strcmp(s, "idle") == 0) return ST_IDLE;
    if (strcmp(s, "wait") == 0) return ST_WAIT;
    if (strcmp(s, "err") == 0)  return ST_ERR;
    if (strcmp(s, "off") == 0)  return ST_OFF;
    return ST_UNKNOWN;
}

static volatile bool s_usb_ready = false;

static void copy_str(char *dst, size_t cap, cJSON *item)
{
    if (cJSON_IsString(item)) {
        strncpy(dst, item->valuestring, cap - 1);
        dst[cap - 1] = 0;
    } else {
        dst[0] = 0;
    }
}

static void handle_line(const char *line)
{
    cJSON *root = cJSON_Parse(line);
    if (!root) return;

    cJSON *jag = cJSON_GetObjectItem(root, "ag");
    if (!cJSON_IsString(jag)) { cJSON_Delete(root); return; }
    int idx = agent_index(jag->valuestring);
    if (idx < 0) { cJSON_Delete(root); return; }

    agent_t *a = &s_agent[idx];
    cJSON *st = cJSON_GetObjectItem(root, "st");
    a->st = parse_state(cJSON_IsString(st) ? st->valuestring : NULL);

    copy_str(a->task, sizeof(a->task), cJSON_GetObjectItem(root, "task"));
    copy_str(a->ago,  sizeof(a->ago),  cJSON_GetObjectItem(root, "ago"));
    copy_str(a->cmd,  sizeof(a->cmd),  cJSON_GetObjectItem(root, "cmd"));
    copy_str(a->csub, sizeof(a->csub), cJSON_GetObjectItem(root, "csub"));
    copy_str(a->heat, sizeof(a->heat), cJSON_GetObjectItem(root, "heat"));

    cJSON *jel = cJSON_GetObjectItem(root, "el");
    a->elapsed = cJSON_IsNumber(jel) ? jel->valueint : -1;

    cJSON *jcnt = cJSON_GetObjectItem(root, "cnt");
    a->cnt = cJSON_IsNumber(jcnt) ? jcnt->valueint : -1;

    cJSON *jcpu = cJSON_GetObjectItem(root, "cpu");
    cJSON *jmem = cJSON_GetObjectItem(root, "mem");
    cJSON *jdsk = cJSON_GetObjectItem(root, "disk");
    cJSON *jld  = cJSON_GetObjectItem(root, "load");
    if (cJSON_IsNumber(jcpu)) a->cpu = jcpu->valueint;
    if (cJSON_IsNumber(jmem)) a->mem = jmem->valueint;
    if (cJSON_IsNumber(jdsk)) a->disk = jdsk->valueint;
    if (cJSON_IsNumber(jld))  a->load10 = jld->valueint;

    a->last_rx = time(NULL);
    cJSON_Delete(root);

    static int ok_count;
    if (++ok_count % 20 == 0) {
        printf("已收 %d 条\n", ok_count);
    }

    if (bsp_display_lock(1000) == ESP_OK) {
        update_page(idx);
        update_page_dots();
        lv_obj_set_style_text_color(s_link, C_IDLE, 0);
        lv_label_set_text(s_link, "LINK OK");
        bsp_display_unlock();
    }

    static char last_desc[4][160];
    char desc[160];
    snprintf(desc, sizeof(desc), "%s / %s", state_word(a->st), a->task);
    if (strcmp(last_desc[idx], desc) != 0) {
        strncpy(last_desc[idx], desc, sizeof(last_desc[0]) - 1);
        last_desc[idx][sizeof(last_desc[0]) - 1] = 0;
        printf("状态变化 %s：%s\n", a->id, desc);
        if (s_usb_ready) {
            char ack[192];
            int alen = snprintf(ack, sizeof(ack), "ACK %s %s\n", a->id, desc);
            if (alen > 0) {
                tinyusb_cdcacm_write_queue(TINYUSB_CDC_ACM_0, (const uint8_t *)ack, (size_t)alen);
                tinyusb_cdcacm_write_flush(TINYUSB_CDC_ACM_0, 0);
            }
        }
    }
}

static SemaphoreHandle_t s_line_mtx;

static void push_line(const char *line)
{
    if (s_line_mtx && xSemaphoreTake(s_line_mtx, pdMS_TO_TICKS(300)) == pdTRUE) {
        handle_line(line);
        xSemaphoreGive(s_line_mtx);
    }
}

static void serial_task(void *arg)
{
    (void)arg;
    static char line[256];
    int n = 0;
    char chunk[128];

    while (1) {
        int got = read(0, chunk, sizeof(chunk) - 1);
        if (got <= 0) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        for (int i = 0; i < got; i++) {
            char c = chunk[i];
            if (c == '\n' || c == '\r') {
                if (n > 0) { line[n] = 0; n = 0; push_line(line); }
            } else if (n < (int)sizeof(line) - 1) {
                line[n++] = c;
            } else {
                n = 0;
            }
        }
    }
}

static void usb_rx_task(void *arg)
{
    (void)arg;
    static char line[256];
    int n = 0;
    uint8_t chunk[512];

    while (1) {
        size_t rx = 0;
        esp_err_t ret = tinyusb_cdcacm_read(TINYUSB_CDC_ACM_0, chunk, sizeof(chunk), &rx);
        if (ret == ESP_OK && rx > 0) {
            for (size_t i = 0; i < rx; i++) {
                char c = (char)chunk[i];
                if (c == '\n' || c == '\r') {
                    if (n > 0) { line[n] = 0; n = 0; push_line(line); }
                } else if (n < (int)sizeof(line) - 1) {
                    line[n++] = c;
                } else {
                    n = 0;
                }
            }
        } else {
            vTaskDelay(pdMS_TO_TICKS(15));
        }
    }
}

static void stale_timer(lv_timer_t *t)
{
    (void)t;
    time_t now = time(NULL);
    bool changed = false;

    for (int i = 0; i < 4; i++) {
        agent_t *a = &s_agent[i];
        if (a->last_rx && now - a->last_rx > 15 && a->st != ST_OFF) {
            a->st = ST_OFF;
            a->elapsed = -1;
            strcpy(a->task, "超过 15 秒未收到数据");
            a->cmd[0] = 0;
            changed = true;
        }
    }

    bool all_dead = true;
    for (int i = 0; i < 4; i++) {
        if (now - s_agent[i].last_rx <= 15) { all_dead = false; break; }
    }
    if (all_dead) {
        lv_obj_set_style_text_color(s_link, C_FAIL, 0);
        lv_label_set_text(s_link, "NO LINK");
    }
    if (changed) update_all_pages();
}

void app_main(void)
{
    printf("\n=== 智能体副屏 V2：四页版 ===\n");

    lv_display_t *disp = bsp_display_start();
    if (disp == NULL) {
        printf("显示初始化失败\n");
        return;
    }
    if (bsp_display_lock(2000) != ESP_OK) {
        printf("获取锁失败\n");
        return;
    }
    build_ui();
    lv_timer_create(stale_timer, 1000, NULL);
    bsp_display_unlock();

    bsp_display_brightness_set(100);

    s_line_mtx = xSemaphoreCreateMutex();
    xTaskCreate(serial_task, "panel_rx", 8192, NULL, 5, NULL);

    const tinyusb_config_t tusb_cfg = TINYUSB_DEFAULT_CONFIG();
    esp_err_t usb_ret = tinyusb_driver_install(&tusb_cfg);
    if (usb_ret == ESP_OK) {
        tinyusb_config_cdcacm_t acm_cfg = {
            .cdc_port = TINYUSB_CDC_ACM_0,
            .callback_rx = NULL,
            .callback_rx_wanted_char = NULL,
            .callback_line_state_changed = NULL,
            .callback_line_coding_changed = NULL,
        };
        usb_ret = tinyusb_cdcacm_init(&acm_cfg);
    }
    printf("USB CDC 初始化：%s\n", usb_ret == ESP_OK ? "成功" : "失败");
    if (usb_ret == ESP_OK) {
        s_usb_ready = true;
        xTaskCreate(usb_rx_task, "panel_usb", 8192, NULL, 5, NULL);
    }

    printf("=== 就绪：等待 UART 或 USB CDC 的 NDJSON ===\n");
}
