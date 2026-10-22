set(FONT_SRCS
    lv_font_noto30.c
    lv_font_noto16.c
    lv_font_orbitron48.c
    lv_font_orbitron32.c
    lv_font_orbitron20.c)

foreach(f ${FONT_SRCS})
    if(NOT EXISTS "${CMAKE_CURRENT_LIST_DIR}/${f}")
        message(FATAL_ERROR
            "字体源文件缺失：main/${f}\n"
            "这些文件由工具生成，不纳入版本库。请先运行：\n"
            "    python3 scripts/gen_fonts.py\n"
            "详见 docs/build.md")
    endif()
endforeach()

idf_component_register(SRCS "main.c"
                            "lv_img_agent_icons.c"
                            ${FONT_SRCS}
                       PRIV_REQUIRES esp_psram json esp_tinyusb)
