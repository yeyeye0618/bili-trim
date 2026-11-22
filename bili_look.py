# -*- coding: utf-8 -*-
"""
bili_look.py — 介面的樣子：顏色、字級、主題。

整個介面是一張攤在桌上的稿紙：偏冷的白紙、印上去的綠格、硃砂紅的刪除號。
影格放在一口墨井裡，四周留黑，看畫面的時候眼睛周圍還是暗的。

改的時候守住這幾條：
  * 硃砂紅只給兩件事：會被移除的段落、不可逆的動作（寫入手機、還原到手機）。
    錯誤和警告都不用紅色 —— 紅色一多，刪除號就不醒目了。
  * 沒有圓角、陰影、漸層。紙上沒有那些東西。
  * 次要文字用從紙色調出來的褐灰（INK_2），不是中性灰。對紙底的對比在 6:1 以上。
  * 時間、位元組這些會跳動的數字用數字字型（等寬數字），不然播放時整排會抖。

之後要做深色主題，只要換這個檔案裡的顏色。
"""

import dearpygui.dearpygui as dpg

# --------------------------------------------------------------------------- #
# 顏色
# --------------------------------------------------------------------------- #

PAPER = (243, 242, 236, 255)        # 稿紙：偏冷的白，不是奶油色
PAPER_2 = (231, 230, 221, 255)      # 紙的陰面：資訊欄、按鈕、表頭
PAPER_3 = (210, 209, 198, 255)      # 壓線、分隔
SELECTED = (221, 220, 209, 255)     # 清單裡選中的那一列
GRID = (95, 143, 108, 255)          # 稿紙印的綠格（外框、刻度線）
GRID_SOFT = (168, 196, 174, 255)    # 格子裡的細線：一格一個 fragment
INK = (23, 20, 15, 255)             # 黑字
INK_2 = (84, 84, 74, 255)           # 次要字：從紙色調出來的褐灰
INK_3 = (150, 148, 136, 255)        # 停用的字（不必達到內文對比）
VERMILION = (184, 35, 26, 255)      # 硃砂：刪除、不可逆
VERMILION_DEEP = (150, 28, 20, 255)
VERMILION_PALE = (242, 207, 201, 255)
WELL = (18, 16, 16, 255)            # 影格的墨井
ON_WELL = (217, 210, 196, 255)
ON_WELL_2 = (154, 145, 131, 255)
OK = (42, 115, 71, 255)             # 狀態燈與「可以」：從格線的綠壓深
WARN = (125, 95, 14, 255)           # 提醒：赭色，不是紅
CLEAR = (0, 0, 0, 0)


def alpha(c, a):
    return (c[0], c[1], c[2], a)


# --------------------------------------------------------------------------- #
# 字級
# --------------------------------------------------------------------------- #
#
# 以使用者選的「介面字級」為 17 來寫，實際大小由 GUI 的 px() 換算（DPI 與字級
# 倍率都在裡面）。Dear PyGui 的字級是行高，跟 CSS 的 px 不同：同樣標 17，
# Noto 的中文字寬大約只有 14 px。

SIZES = {
    "small": 16,     # 說明、清單的第二行、訊息
    "body": 17,      # 內文、按鈕
    "lead": 18,      # 時間碼後面那段
    "value": 17,     # 資訊欄數值裡的數字（Barlow 的數字跟 Noto 的中文同字級剛好搭）
    "action": 20,    # 「備份並寫入手機」
    "head": 24,      # 區塊標題（襯線）
    "mark": 28,      # BILI-TRIM
    "clock": 46,     # 播放位置
}

# 字型角色 -> (字型組裡的哪一個, 字級)
FONT_ROLES = {
    "body": ("body", "body"),
    "small": ("body", "small"),
    "lead": ("body", "lead"),
    "strong": ("strong", "body"),
    "strong_s": ("strong", "small"),
    "action": ("strong", "action"),
    "head": ("serif", "head"),
    "title": ("title", "body"),
    "num": ("num", "body"),
    "num_s": ("num", "small"),
    "num_l": ("num", "lead"),
    "num_v": ("numr", "value"),    # 資訊欄的數值：一般體，跟中文同一個粗細
    "clock": ("num", "clock"),
    "mark": ("num", "mark"),
}


# --------------------------------------------------------------------------- #
# 主題
# --------------------------------------------------------------------------- #

def _colors(pairs):
    for col, value in pairs:
        dpg.add_theme_color(col, value)


def _styles(pairs):
    for var, *value in pairs:
        dpg.add_theme_style(var, *value)


def make_themes(px):
    """
    建全部的主題，回傳 {名稱: theme id}。px 是 GUI 的邏輯 -> 實體像素換算。
    換字級時整組重建（間距都跟著字級縮放）。
    """
    T = {}
    C = dpg

    # ---- 全域：一張紙 ----
    with dpg.theme() as t:
        with dpg.theme_component(dpg.mvAll):
            _colors((
                (C.mvThemeCol_Text, INK),
                (C.mvThemeCol_TextDisabled, INK_2),
                (C.mvThemeCol_WindowBg, PAPER),
                (C.mvThemeCol_ChildBg, CLEAR),
                (C.mvThemeCol_PopupBg, PAPER),
                (C.mvThemeCol_Border, PAPER_3),
                (C.mvThemeCol_BorderShadow, CLEAR),
                (C.mvThemeCol_FrameBg, PAPER_2),
                (C.mvThemeCol_FrameBgHovered, (222, 221, 211, 255)),
                (C.mvThemeCol_FrameBgActive, (214, 213, 202, 255)),
                (C.mvThemeCol_TitleBg, PAPER_2),
                (C.mvThemeCol_TitleBgActive, PAPER_3),
                (C.mvThemeCol_TitleBgCollapsed, PAPER_2),
                (C.mvThemeCol_MenuBarBg, PAPER),
                (C.mvThemeCol_ScrollbarBg, CLEAR),
                (C.mvThemeCol_ScrollbarGrab, PAPER_3),
                (C.mvThemeCol_ScrollbarGrabHovered, (189, 188, 176, 255)),
                (C.mvThemeCol_ScrollbarGrabActive, INK_2),
                (C.mvThemeCol_CheckMark, INK),
                (C.mvThemeCol_SliderGrab, INK),
                (C.mvThemeCol_SliderGrabActive, INK_2),
                (C.mvThemeCol_Button, PAPER_2),
                (C.mvThemeCol_ButtonHovered, (220, 219, 208, 255)),
                (C.mvThemeCol_ButtonActive, PAPER_3),
                (C.mvThemeCol_Header, SELECTED),
                (C.mvThemeCol_HeaderHovered, PAPER_2),
                (C.mvThemeCol_HeaderActive, PAPER_3),
                (C.mvThemeCol_Separator, PAPER_3),
                (C.mvThemeCol_SeparatorHovered, INK_2),
                (C.mvThemeCol_SeparatorActive, INK),
                (C.mvThemeCol_ResizeGrip, CLEAR),
                (C.mvThemeCol_ResizeGripHovered, PAPER_3),
                (C.mvThemeCol_ResizeGripActive, INK_2),
                (C.mvThemeCol_PlotHistogram, INK),
                (C.mvThemeCol_PlotHistogramHovered, INK),
                (C.mvThemeCol_TableHeaderBg, PAPER_2),
                (C.mvThemeCol_TableBorderStrong, PAPER_3),
                (C.mvThemeCol_TableBorderLight, PAPER_3),
                (C.mvThemeCol_TableRowBg, CLEAR),
                (C.mvThemeCol_TableRowBgAlt, CLEAR),
                (C.mvThemeCol_TextSelectedBg, alpha(GRID_SOFT, 170)),
                (C.mvThemeCol_DragDropTarget, INK),
                (C.mvThemeCol_NavHighlight, INK),
                (C.mvThemeCol_ModalWindowDimBg, (18, 16, 16, 110)),
            ))
            # 輸入框的游標有自己的顏色。沒設的話留著 Dear PyGui 深色主題的預設
            # （接近白色），在紙色底上幾乎看不見 —— 使用者以為輸入框不能打字
            if hasattr(C, "mvThemeCol_InputTextCursor"):
                dpg.add_theme_color(C.mvThemeCol_InputTextCursor, INK)
            _styles((
                (C.mvStyleVar_WindowRounding, 0),
                (C.mvStyleVar_ChildRounding, 0),
                (C.mvStyleVar_FrameRounding, 0),
                (C.mvStyleVar_PopupRounding, 0),
                (C.mvStyleVar_ScrollbarRounding, 0),
                (C.mvStyleVar_GrabRounding, 0),
                (C.mvStyleVar_TabRounding, 0),
                (C.mvStyleVar_WindowBorderSize, 1),
                (C.mvStyleVar_ChildBorderSize, 1),
                (C.mvStyleVar_PopupBorderSize, 1),
                (C.mvStyleVar_FrameBorderSize, 1),
                (C.mvStyleVar_WindowPadding, px(16), px(13)),
                (C.mvStyleVar_FramePadding, px(10), px(6)),
                (C.mvStyleVar_ItemSpacing, px(7), px(7)),
                (C.mvStyleVar_ItemInnerSpacing, px(7), px(5)),
                (C.mvStyleVar_CellPadding, px(12), px(6)),
                (C.mvStyleVar_ScrollbarSize, px(11)),
                (C.mvStyleVar_GrabMinSize, px(10)),
                (C.mvStyleVar_IndentSpacing, px(16)),
                (C.mvStyleVar_WindowTitleAlign, 0.0, 0.5),
                (C.mvStyleVar_ButtonTextAlign, 0.5, 0.5),
                (C.mvStyleVar_SelectableTextAlign, 0.0, 0.5),
            ))
            if hasattr(C, "mvStyleVar_DisabledAlpha"):
                # 停用的樣子由下面的主題元件決定，不要再整個變淡一次
                dpg.add_theme_style(C.mvStyleVar_DisabledAlpha, 1.0)
        with dpg.theme_component(dpg.mvButton, enabled_state=False):
            _colors(((C.mvThemeCol_Button, PAPER_2),
                     (C.mvThemeCol_ButtonHovered, PAPER_2),
                     (C.mvThemeCol_ButtonActive, PAPER_2),
                     (C.mvThemeCol_Text, INK_3)))
        with dpg.theme_component(dpg.mvSelectable, enabled_state=False):
            _colors(((C.mvThemeCol_Text, INK_3),))
        with dpg.theme_component(dpg.mvInputText, enabled_state=False):
            _colors(((C.mvThemeCol_Text, INK_3),))
    T["global"] = t

    # ---- 版面骨架：最外層不留邊，欄位自己決定內距 ----
    def frame(name, pad, spacing, bg=None, border=None, text_flush=False):
        # text_flush：Dear PyGui 的文字一律往下推 FramePadding.y（為了跟同一行的
        # 按鈕對齊）。自己算座標排版的區塊把它歸零，座標才會是字真正的位置。
        with dpg.theme() as th:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(C.mvStyleVar_WindowPadding, *pad)
                dpg.add_theme_style(C.mvStyleVar_ItemSpacing, *spacing)
                if text_flush:
                    dpg.add_theme_style(C.mvStyleVar_FramePadding, px(10), 0)
                dpg.add_theme_style(C.mvStyleVar_WindowBorderSize, 0)
                dpg.add_theme_style(C.mvStyleVar_ChildBorderSize,
                                    1 if border else 0)
                if bg is not None:
                    dpg.add_theme_color(C.mvThemeCol_ChildBg, bg)
                if border is not None:
                    dpg.add_theme_color(C.mvThemeCol_Border, border)
        T[name] = th

    frame("root", (0, 0), (0, 0))
    frame("bar", (px(20), px(8)), (px(12), 0), text_flush=True)
    frame("pane", (0, 0), (0, 0), text_flush=True)       # 清單欄：列自己排
    frame("work", (px(19), px(15)), (px(7), px(10)))
    frame("insp", (0, 0), (0, 0), bg=PAPER_2)
    frame("insp_info", (px(16), px(13)), (px(7), px(10)))
    frame("insp_act", (px(16), 0), (px(7), px(10)))
    frame("foot", (px(20), px(6)), (px(14), 0), text_flush=True)
    frame("tight", (0, 0), (0, 0), text_flush=True)  # 往下推一點、對齊基線用
    frame("well", (0, 0), (0, 0), bg=WELL, border=INK)
    frame("log", (0, 0), (0, px(4)), text_flush=True)
    frame("kv", (0, 0), (0, px(12)), text_flush=True)
    frame("cuts", (0, 0), (0, 0), border=PAPER_3, text_flush=True)

    # 墨井右下角那張小標籤
    with dpg.theme() as th:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(C.mvThemeCol_ChildBg, (10, 9, 8, 215))
            dpg.add_theme_color(C.mvThemeCol_Text, ON_WELL)
            dpg.add_theme_style(C.mvStyleVar_WindowPadding, px(9), px(4))
            dpg.add_theme_style(C.mvStyleVar_ItemSpacing, px(6), 0)
            dpg.add_theme_style(C.mvStyleVar_ChildBorderSize, 0)
    T["well_tag"] = th

    # ---- 按鈕 ----
    def button(name, bg, hover, active, text, border=None, border_size=1,
               off_bg=PAPER_2, off_text=INK_3, pad=None):
        # 停用狀態要另外寫一份：enabled_state=False 的元件不會繼承上面那份的
        # 邊框和內距，不寫的話停用的按鈕會變回全域樣式（多一圈框、變大）
        with dpg.theme() as th:
            for enabled in (True, False):
                kind = dpg.mvAll if enabled else dpg.mvButton
                with dpg.theme_component(kind, enabled_state=enabled):
                    if enabled:
                        _colors(((C.mvThemeCol_Button, bg),
                                 (C.mvThemeCol_ButtonHovered, hover),
                                 (C.mvThemeCol_ButtonActive, active),
                                 (C.mvThemeCol_Text, text)))
                    else:
                        _colors(((C.mvThemeCol_Button, off_bg),
                                 (C.mvThemeCol_ButtonHovered, off_bg),
                                 (C.mvThemeCol_ButtonActive, off_bg),
                                 (C.mvThemeCol_Text, off_text)))
                    dpg.add_theme_style(C.mvStyleVar_FrameBorderSize,
                                        border_size)
                    if border is not None:
                        dpg.add_theme_color(C.mvThemeCol_Border,
                                            border if enabled else PAPER_3)
                    if pad is not None:
                        dpg.add_theme_style(C.mvStyleVar_FramePadding, *pad)
        T[name] = th

    # 墨色：主要但可逆（播放、載入）
    button("ink", INK, (52, 48, 41, 255), (0, 0, 0, 255), PAPER, border=INK)
    # 硃砂：不可逆（寫入手機、還原到手機）
    button("write", VERMILION, VERMILION_DEEP, (122, 22, 16, 255),
           (255, 255, 255, 255), border=VERMILION, off_bg=PAPER_3,
           off_text=INK_2)
    # 線框：次要動作
    button("ghost", CLEAR, PAPER_2, PAPER_3, INK, border=INK_2,
           off_bg=CLEAR)
    # 區間表草稿列裡的線框鈕：矮一點，跟同一列的文字一樣高
    button("ghost_s", CLEAR, PAPER_2, PAPER_3, INK, border=INK_2,
           off_bg=CLEAR, pad=(px(10), px(2)))
    button("ink_s", INK, (52, 48, 41, 255), (0, 0, 0, 255), PAPER, border=INK,
           pad=(px(10), px(2)))
    # 純文字：頂列選單、表格裡的「看」
    button("flat", CLEAR, PAPER_2, PAPER_3, INK, border_size=0,
           off_bg=CLEAR, pad=(px(8), px(5)))
    # 表格裡的小動作（看、取消、清除）：跟文字一樣高
    button("link", CLEAR, PAPER_2, PAPER_3, INK, border_size=0,
           off_bg=CLEAR, pad=(px(6), px(1)))
    # 頂列的晶片（裝置）
    button("chip", PAPER_2, (220, 219, 208, 255), PAPER_3, INK,
           border_size=0)
    # 切換鈕：選中的是墨底白字
    button("seg_on", INK, INK, INK, PAPER, border=INK_2)
    button("seg_off", CLEAR, PAPER_2, PAPER_3, INK_2, border=INK_2,
           off_bg=CLEAR)
    # 清單底下的整列按鈕
    button("outline", CLEAR, PAPER_2, PAPER_3, INK, border=INK, off_bg=CLEAR)

    # ---- 輸入框 ----
    with dpg.theme() as th:          # 搜尋框
        with dpg.theme_component(dpg.mvAll):
            _colors(((C.mvThemeCol_FrameBg, PAPER_2),
                     (C.mvThemeCol_FrameBgHovered, (236, 235, 227, 255)),
                     (C.mvThemeCol_FrameBgActive, (250, 249, 245, 255)),
                     (C.mvThemeCol_Border, PAPER_3)))
            dpg.add_theme_style(C.mvStyleVar_FramePadding, px(10), px(7))
    T["search"] = th

    with dpg.theme() as th:          # 表格裡的時間：平常像字，滑過才看得出能改
        with dpg.theme_component(dpg.mvAll):
            _colors(((C.mvThemeCol_FrameBg, CLEAR),
                     (C.mvThemeCol_FrameBgHovered, PAPER_2),
                     (C.mvThemeCol_FrameBgActive, (250, 249, 245, 255))))
            dpg.add_theme_style(C.mvStyleVar_FrameBorderSize, 0)
            dpg.add_theme_style(C.mvStyleVar_FramePadding, px(4), px(3))
    T["cell"] = th

    with dpg.theme() as th:          # 正在改的那格：白底墨框，一眼看得出在改哪裡
        with dpg.theme_component(dpg.mvAll):
            _colors(((C.mvThemeCol_FrameBg, (255, 255, 255, 255)),
                     (C.mvThemeCol_FrameBgHovered, (255, 255, 255, 255)),
                     (C.mvThemeCol_FrameBgActive, (255, 255, 255, 255)),
                     (C.mvThemeCol_Border, INK)))
            dpg.add_theme_style(C.mvStyleVar_FrameBorderSize, 1)
            dpg.add_theme_style(C.mvStyleVar_FramePadding, px(4), px(3))
    T["cell_on"] = th

    # ---- 進度條：墨色填進紙槽 ----
    with dpg.theme() as th:
        with dpg.theme_component(dpg.mvAll):
            _colors(((C.mvThemeCol_FrameBg, PAPER_3),
                     (C.mvThemeCol_PlotHistogram, INK)))
            dpg.add_theme_style(C.mvStyleVar_FrameBorderSize, 0)
            dpg.add_theme_style(C.mvStyleVar_FramePadding, 0, 0)
    T["progress"] = th

    return T
