# bili-trim

把 B站 Android 緩存影片裡的工商片段剪掉，**剪完還是在 B站 App 裡播** ——
播放器、彈幕、進度記憶全部照舊。

不是轉檔、不是另存，是直接在 App 的緩存檔上動刀，所以 App 認得它。

*Cuts sponsor segments out of Bilibili's Android offline cache in place, so the
videos still play inside the Bilibili app — same player, same danmaku, same
resume position. Chinese-only UI and docs.*

```
bili-trim.bat   點兩下就開始（找 Python → run.py）
run.py          入口：驗 Python 版本、補缺的套件，然後開視窗
setup.py        命令列版的安裝，另外負責打包帶去別台電腦
bili_gui.py     圖形介面（挑廣告、即時預覽、一鍵寫回手機）
bili_trim.py    剪輯引擎 + 命令列版本（純標準庫，不需要 ffmpeg）
bili_device.py  adb：找裝置、找 B站 路徑、列緩存、拉取／推送
bili_backup.py  單檔備份（.bilibak）與還原
bili_media.py   預覽用的影格來源與播放器（ffmpeg / ffplay）
bili_font.py    把系統字型合併成一個不缺字的字型檔
bili_env.py     找 adb / ffmpeg（先看 tools/，再看 PATH）與環境自我檢查
bili_config.py  設定檔（備份位置等），setup.py 會先幫你寫好
```

---

## 先說清楚

- 這個工具會**直接改 B站 App 的緩存檔**。寫入前一定會先備份一份乾淨原檔
  （不是選項，是強制的），但請自己確認那份 `.bilibak` 真的在。
- 跟 B站 官方沒有任何關係，也沒有用到任何官方介面 —— 它只是讀寫
  手機上已經存在的檔案。
- 請只對**你自己在 App 裡下載的內容**使用。剪掉的片段不會離開你的手機，
  這個工具不會上傳、不會解密、不會繞過 DRM，也不幫你把影片存出來。
- **B站 App 更新之後行為可能改變。** 下面那張表是在某個版本上實測的結果，
  不是官方保證。覺得不對就先用 `--apply` 之外的乾跑模式看數字。

### 測過的環境

| | |
| --- | --- |
| 開發與實測 | Windows 11、Python 3.14、一支 Android 手機（國際版 Bilibili） |
| 寫得出來但沒實測 | macOS、Linux（路徑與下載來源都寫好了，沒有機器驗過） |
| 沒支援 | 舊版 FLV 分段緩存、沒下載完的緩存 |

---

## 為什麼是這種做法

這幾點都是逐項二分搜尋實測出來的，不是推測。測試方式是每次只改一個變數、
推回手機、用 logcat 依 cid 判定播放結果，每次推送前都在裝置上重算 md5 核對。

| 對緩存檔做的事 | B站 App 的反應 |
| --- | --- |
| 改內容位元組（翻轉 1 byte） | 可以播 |
| 檔尾附加位元組（檔案變大） | 可以播 |
| 改 `entry.json` 的 `total_bytes` / 時長 | 可以播 |
| 改檔案內的 `mvhd` / `tkhd` / `mehd` | 可以播 |
| 改 `moof` 裡的時間數值（`tfdt` / `trun`） | 可以播 |
| 減少 `sidx` 的參考項（大小不變） | 可以播 |
| **ffmpeg 重新封裝（即使完全不剪）** | **卡在「正在緩衝」** |
| **移除 fragment 讓檔案變小** | **卡在「正在緩衝」** |

結論兩點：

1. **不能用 ffmpeg 重封裝。** 它的 `+empty_moov` 不寫 `mehd`/`elst`、
   `mvhd`/`tkhd` 時長留 0、`sidx` 寫成 version 1 —— 整體就是跟 B站 不同的檔案。
   所以本工具完全不靠 ffmpeg 改容器，自己讀寫 box。
2. **檔案不能比原本小。** 推測是下載管理器拿它資料庫裡記的大小比對，發現比較小
   就判定下載不完整、去向 CDN 續傳，而那些網址早就過期了（`deadline=…`），
   於是永遠緩衝、而且不會報網路錯誤。
   所以移除 fragment 之後，尾端補一個 `free` box 把大小補回原值。

### 代價

- **手機容量不會變少。** 廣告是真的被移除了（時間軸連續接起來、時長真的變短），
  但檔案大小一模一樣。這是上面第 2 點換來的。
- **視訊切點精度 = GOP 長度（B站 大約 5 秒）。** 每個 fragment 開頭才是關鍵影格，
  只能整塊移除。GUI 的時間軸下方那排細刻度就是 fragment 邊界，放大就看得到
  切點會被吸附到哪裡。音訊可以精切（約 23 ms），所以音畫不會飄。
- **剪過的可以再剪，但時間是以目前這一版為準。** 剪完時間軸就接起來了，
  原本 10:00 的畫面現在在 8:00。工具認得出「剪過的檔案」（尾端那塊補大小用的
  `free` box）並會提醒你，但不會擋你 —— 備份是後悔藥，不該是編輯的前提。
  要小心的只有一件事：**別把上次用過的切點時間再貼一次**，那些位置現在已經
  是正片了。在 GUI 裡拖時間軸挑不會有這個問題，因為你看到的就是目前這一版。

---

## 開始用

1. 下載這個專案：**Code → Download ZIP**，解壓到你自己的資料夾
   （桌面、文件都可以，不要放 `Program Files`，那裡寫不進去）
2. **點兩下 `bili-trim.bat`**
3. 第一次會問要不要裝三個 Python 套件，按 Enter 就好
4. 程式開起來。少了 `adb` 或 `ffmpeg` 的話，它會自己跳出**環境檢查**視窗，
   列出缺什麼、去哪裡下載、放到哪個資料夾，旁邊就是按鈕
5. 手機接上電腦、開「USB 偵錯」，按**重新掃描**

沒有 Python 的話，第 2 步會告訴你去裝；裝完再點一次同一個檔案。
（`winget install -e --id Python.Python.3.12`，或到
[python.org](https://www.python.org/downloads/) 下載，安裝畫面最下面
記得勾 **Add python.exe to PATH**。）

### 要自己準備的兩個外部程式

這兩個不是 pip 裝得到的東西，請自己下載 —— 程式不會偷偷幫你抓：

| | 去哪裡拿 | 放哪裡 | 沒有的話 |
| --- | --- | --- | --- |
| **adb** | [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools) | `tools/` 裡放 `adb.exe` 和旁邊的 `AdbWinApi.dll`、`AdbWinUsbApi.dll` | 完全不能用 |
| **ffmpeg** | [ffmpeg.org](https://ffmpeg.org/download.html) | `tools/` 裡放 `ffmpeg`、`ffplay`、`ffprobe` 三個執行檔 | 預覽不能用，剪輯還是可以 |

**不用改 PATH。** 程式先看專案裡的 `tools/`，再看 PATH ——
已經有 adb／ffmpeg 的人不受影響，沒有的人也不必去翻「系統環境變數」。
那是這個專案以前最大的摩擦：下載、解壓、新增一列、重開終端機，
任何一步漏掉，看到的都只是一句「找不到 adb」，而且看不出是哪一步錯。

要放在別的地方也行：環境變數 `BILI_TOOLS` 指到那個資料夾，或是
`BILI_ADB` / `BILI_FFMPEG` / `BILI_FFPLAY` / `BILI_FFPROBE` 直接指到執行檔。

### 不想一步一步來

```bash
python setup.py
```

命令列版的安裝，一路做到「接上手機、影片列得出來」為止。比點兩下多做這些事：

| 步驟 | 內容 |
| --- | --- |
| Python 套件 | 裝到「現在跑的這個」直譯器 |
| 外部工具 | **問過你之後**才從官方來源下載，放進 `tools/` |
| 字型 | 先把韓文與符號合併進中文字型（約半分鐘），省得第一次開程式時等 |
| 啟動方式 | 把它用的那個 Python 記進 `python-path.txt`，啟動檔會優先讀它 |
| 設定 | 問備份位置、建好資料夾、寫進設定檔 |
| 手機 | 等你把 USB 偵錯打開並授權，然後找 B站 的緩存目錄、列一次影片 |

最後一步會**一直等**，不是檢查一次就放棄 —— 使用者的時間都花在手機上
（連點七次版本號碼、翻開發人員選項、按授權對話框），檢查一次就說
「沒偵測到手機」等於叫人自己重跑。沒接手機、接了沒授權、offline，
三種狀態的下一步都不一樣，所以會分開講。它也會分清楚「手機上沒裝 B站」
和「裝了但下載目錄還不存在」（目錄要在 App 裡下載過一支影片才會建出來）。

`python-path.txt` 這件事是因為一台電腦常常有好幾個 Python（市集版、
python.org 版、conda…）。套件裝在哪一個就要用哪一個開，不然就會
「明明裝好了卻說找不到 dearpygui」。

其他選項：`--check` 只檢查不動手、`--yes` 所有詢問都當同意、`--offline`
不下載、`--skip-font` 不建字型、`--backup-dir PATH` 直接指定備份位置、
`--pack` 打包（見下一節）。

### 帶去另一台電腦

```bash
python setup.py --pack
```

產生 `bili-trim-portable.zip`，**連 `tools/` 一起包進去**（加 `--no-tools`
可以不包）。在新電腦解壓之後點兩下 `bili-trim.bat`，因為工具已經在裡面了，
不用再下載一次。

> 這個包是給自己搬家用的，不適合公開散佈 —— 裡面的 ffmpeg 多半是 GPL
> 授權的建置，platform-tools 也有自己的條款，跟本專案的 MIT 不是同一回事。

### 出問題的時候

程式裡：**說明 → 環境檢查**。逐項列出 Python、三個套件、
adb／ffmpeg／ffplay／ffprobe、合併字型、備份位置，缺的東西下面就是下載
連結和「開啟 tools 資料夾」按鈕。按**把結果寫進訊息區**可以複製整段貼出來。

命令列：

```bash
python setup.py --check
```

同一份檢查，外加看一次手機：連得上就把緩存目錄和影片數量也列出來。

兩個刻意不偷懶的地方：

- **工具會真的執行一次問版本**，不只看檔案在不在。被防毒軟體隔離、
  抄到一半、少了旁邊的 dll，這些情況檔案都還在，但一跑就死。
- **套件是開一個新的行程去 import**。`pip --user` 的目標目錄如果在啟動時
  還不存在，Python 就不會把它放進 `sys.path`，於是同一個行程裝完照樣
  `import` 不到 —— 在自己這裡判斷會得到「剛裝好卻說沒裝」的錯誤結論。

### 關於字型

B站 的標題什麼字都有：繁中、簡中、假名、韓文，還有 `♡ ❤ ┊ ⌇` 和 `💓 💦`。
Android 顯示得出來是因為系統有字型後備鏈，但 Dear PyGui 一次只綁一個字型檔，
缺的字會畫成 `?` —— 而且掃過本機所有大字型，涵蓋最好的也還缺 7 個字。

所以會把韓文與符號從其他系統字型子集化之後**合併成一個字型檔**（約半分鐘，
之後快取起來不用再做）。第一次開程式它會在背景自己建；`setup.py` 會先做好。
這需要 `fonttools`，沒裝也能跑，只是那些字會顯示成 `?`。合併結果放在
`%APPDATA%\bili_trim_gui\fonts\`。

還是看到 `?` 的話，跑這個會告訴你卡在哪裡 —— 用的是哪個 Python、補字字型
建好了沒、以及手機上每一筆標題還缺哪些字元：

```bash
bili-trim.bat --check-font
```

---

## 用法

### 圖形介面

點兩下 `bili-trim.bat`（或 `python run.py`）。

1. 接上手機，按**重新掃描** —— 會自己找 B站 的緩存目錄、列出所有影片
2. 選一支，按**載入以編輯**（從手機拉到本機暫存，之後重用不必再拉）
3. 拖時間軸找廣告：滾輪縮放、拖空白處平移、點一下跳到該處，
   `I` / `O` 設起訖點，**加入移除區間**
4. 切到**成品**模式按播放 —— 播到接縫會直接跳過被移除的段落，
   聽得出來接得乾不乾淨
5. 微調：拖紅塊邊緣，或在下面的清單裡直接打時間
6. **寫入手機** —— 會先強制備份，然後推回去並在裝置上重算 md5 核對

快速鍵：`空白鍵` 播放／暫停、`I` 設起點、`O` 設終點、`←` `→` 單格移動、`Home` 回開頭。

**設定**選單裡可以調：音量、音訊延遲、預覽畫質、**介面字級**（小／標準／大／
特大／超大）。字級改了會連按鈕、欄位、時間軸一起放大，不會只有字變大然後被
切掉；選取的影片、剪輯區間、播放位置都會保留。設定記在
`%APPDATA%\bili_trim_gui\settings.json`。

### 命令列

```bash
# 乾跑，只看會改什麼
python bili_trim.py <cid 目錄> --cut 195-247 --cut 10:12-11:00

# 直接對手機上的路徑操作
python bili_trim.py --device /sdcard/Android/data/com.bilibili.app.in/download/<avid>/<cid> \
                    --cut 195-247 --apply
```

主要選項：`--snap {outer,inner}` 切點吸附方向、`--preview-seams [秒數]` 擷取接縫
對照片段、`--refresh` 忽略暫存重新拉、`--allow-incomplete` 連沒下載完的也剪。

---

## 支援的 B站 版本

| 套件名 | 版本 |
| --- | --- |
| `com.bilibili.app.in` | 國際版 Bilibili |
| `tv.danmaku.bili` | 大陸版 哔哩哔哩 |
| `com.bilibili.app.blue` | 概念版 哔哩哔哩 |

每個套件都會試這些位置（`{pkg}` 代入套件名）：

```
/storage/emulated/0/Android/data/{pkg}/download
/sdcard/Android/data/{pkg}/download
/storage/emulated/0/Android/data/{pkg}/files/download
/sdcard/Android/data/{pkg}/files/download
```

只處理 DASH 緩存（`media_type=2`，有獨立 `video.m4s` / `audio.m4s`）。
舊版的 FLV 分段格式不支援。沒下載完的（`is_completed=false`）會被擋下來。

---

## 備份

寫入前**一定**會先備份，而且備的是乾淨原檔。備份是**一個檔案**，不是資料夾：

```
<備份位置>/<影片標題>.bilibak
```

`.bilibak` 是未壓縮的 zip（影音已經壓縮過，再壓只是浪費時間）：

```
manifest.json          哪支影片、原本在手機哪裡、每個檔案的大小與 md5
payload/entry.json
payload/danmaku.xml
payload/<type_tag>/index.json
payload/<type_tag>/video.m4s
payload/<type_tag>/audio.m4s
```

檔名用影片標題（不是 cid 那串數字），撞名自動加 `(2)`。
還原：**備份 → 還原備份…** → 選一個 → **還原到手機**，會逐檔核對 md5。

> 注意：如果你在 App 裡把那支影片刪掉了，光把檔案放回去沒用 ——
> B站 的資料庫裡沒有那筆記錄。要先在 App 裡重新下載，再還原覆蓋上去。

---

## 本機暫存

`bili_gui_stage/<cid 目錄>/` 是**手機上那一份的鏡像**，旁邊的
`.bili_gui_stage.json` 記著每個檔案的大小、md5，以及這支被剪過幾次。

- 下次載入同一支會先驗這份暫存，驗得過就直接重用，不必再拉幾百 MB
- 手術在另一個 `.out` 目錄做，推成功之後才把暫存更新成新版本
- 所以你在介面上看到、拖曳、預覽的，永遠就是手機上現在那一版 ——
  要再剪一段廣告，時間直接照畫面上標就對了

暫存很佔空間（一支就是原檔大小），不需要了直接刪整個 `bili_gui_stage`。
**但先確認手機上那支還是原檔，或者你已經有 `.bilibak` 備份** ——
不然那可能是你唯一的原檔副本。

---

## 授權

MIT，見 [LICENSE](LICENSE)。
