# agy-bridge

讓 Claude Code 與 Codex 共用 Google Antigravity CLI 的本機派工技能。

## 功能

- 根據任務難度與目前模型／額度分組，自動選擇明確的模型。
- 每次任務使用獨立的檔案副本；CC 與 Codex 可以同時派工。
- 保存結果、修改清單與失敗紀錄，由主代理檢查後整合。
- 關閉超額 AI credits，拒絕 API key／ADC 付費路徑。
- 不會自動合併到原始專案，也不會自動推送或部署。

## 安裝（Windows）

需要 Python 3.10+、已安裝的官方 `agy` CLI，以及已完成 Google 帳號登入。
先檢查安裝程式，再於 repository 目錄執行：

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-AgyBridge.ps1
```

安裝器將技能放入 `~/.agents/skills/agy-bridge/`，並在
`~/.claude/skills/agy-bridge/` 建立指向同一份技能的目錄連結。
它會先備份 agy 設定，關閉 `useG1Credits`，僅新增任務暫存區的檔案寫入授權。
已有同名技能時會停止，避免覆寫。

## 使用

Claude Code：

```text
/agy-bridge 幫這個專案補測試
```

Codex：

```text
$agy-bridge 整理這個專案的架構
```

技能也可在符合條件時自動選用。新安裝後若清單未更新，重新開啟聊天或載入技能。
完整操作與重試規則見 [SKILL.md](agy-bridge/SKILL.md)。

## 測試

測試只使用 Python 標準函式庫，不需要登入、不呼叫模型。

```powershell
python -m unittest discover -s tests -v
```

## 執行範圍

目前以 Windows 本機 CLI 為測試環境。Codex 呼叫 agy 時需要允許登入憑證、
網路與 agy 自身狀態目錄存取。獨立副本是避免工作互相覆蓋的機制，並非 OS 沙盒。
模型與額度仍受 Google 方案限制；額度不足時停止或依技能規則選擇其他可用分組。
此 repository 不包含帳號憑證、本機設定、任務紀錄或帶有個人路徑的驗證報告。

## 參考

派工流程參考 [claude-code-antigravity-bridge](https://github.com/chen941008/claude-code-antigravity-bridge) 的設計，
本套件使用自己的執行腳本與副本工作流程。

- [官方 headless 文件](https://www.antigravity.google/docs/cli/headless/)
- [官方 credits 文件](https://www.antigravity.google/docs/cli/credits/)
