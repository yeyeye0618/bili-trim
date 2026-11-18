---
version: 1
slug: "bili-gui-py"
primary_target: "bili_gui.py"
related_targets: []
---

## Scope

bili_gui.py 的主畫面（編輯一支快取影片）。Visitor mode：Operate。
桌面應用程式，Python + Dear PyGui 2.x，1440×900 以上，字級五段可調。

## Audience / task

一個人、一次專心弄一支。接上手機 → 從清單挑一支 → 載入 → 在時間軸上找工商 →
設起訖 → 試聽接縫 → 寫入手機。偶爾才開一次，不是每天用。

## Direction contract

THESIS: 刪掉一段，而文件還是同一份。這個工具不是剪輯軟體，是校稿 ——
它拒絕 NLE 那套「深灰機艙 + 橘色強調色 + 多軌」的排法，也拒絕淺色 SaaS
儀表板那套圓角卡片。它長得像一張攤在桌上的稿紙，上面放著一張影格。

OWN-WORLD: 中文校對稿。偏冷的白紙（#F3F2EC，不是奶油色）當整個工作檯面，
印綠格（#5F8F6C）切出 fragment，硃砂紅（#B8231A）只給兩件事：會被移除的段落、
不可逆的動作。影格躺在一口墨井（#121210）裡，四周留黑，眼睛的局部環境仍然是暗的。
標題用 Noto Serif TC，介面用 Noto Sans TC，所有時間與位元組用 Barlow Semi
Condensed 的表格數字。沒有圓角、沒有陰影、沒有漸層 —— 紙上沒有那些東西。

STORY: 使用者打開就看到「這是一份可以動刀的稿子」。他在格子上找到工商，
畫上刪除號，右邊的欄位同時把新長度與移除秒數改成紅字；按下寫入之前，
他已經知道會刪掉什麼、檔案大小不會變、備份會先存到哪裡。

FIRST VIEWPORT: 頂端一條 2px 黑線壓住版面，左邊 276px 是快取清單（每列家具固定：
標題兩行、時長、畫質、大小；不能剪的印灰並寫出原因）。中間是墨井裡的影格，
下面一排運鏡鍵與一個 31px 的時間碼。再下面是時間軸：上緣刻度、中間 46px 的
稿紙格、下緣一行說明。右邊 300px 是「這一支」資訊欄（作者、cid、畫質、原長、
新長、移除、視訊／音訊位元組、fragment 數），吸附方向的切換在它下面，
主要動作「備份並寫入手機」是整個畫面唯一的硃砂紅色塊，就在資訊欄裡。

FORM: 使用者指定的組合 —— 校稿的世界（我排序第 1 的 grounded 候選）＋
彈線的排版（骰子指定的第 7 個）＋ 類別標準的右側資訊欄。
seed:9968c061cf7f251c

SIGNATURE MOVE: 刪除號。時間軸的中段是稿紙格，一格一個 fragment；
標起訖之後，被移除的格子淡成硃砂粉紅，上面畫一條帶回鉤的硃砂橫線，
線的正中央寫「刪 52.0 s」。跨不同縮放層級時線會重畫，但永遠對齊格線 ——
「切點只能落在格線上」不需要用文字解釋。

FINISH: unreviewed and undocumented is unfinished; this build ends with the
finish review, the verdict, DESIGN.md, and every shipping raster carrying its
provenance.

## Unresolved

- 要不要留在 Dear PyGui 還沒決定。這個方向在 DPG 裡約九成做得到
  （主題配色、多字型、drawlist 畫稿紙格與刪除號、table 做清單與資訊欄）；
  做不到的是字距（ImGui 沒有 letter-spacing）、虛線框、文字自動截斷，
  這三項都有替代做法，沒有一項是設計的要害。
- 紙色介面配暗影格的長時間疲勞還沒實測。若要退路，同一個世界有暗紙版，
  但那是另一次決策，不是這一版的折衷。
