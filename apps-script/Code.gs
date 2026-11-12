// Stock Board: Google Apps Script timer for the "Update stocks" GitHub workflow.
// Paste this whole file into your Apps Script project (script.google.com), set the GITHUB_TOKEN
// script property, then run installStocksTrigger() once. See README, "Timing (Google Apps Script)".

const STOCKS_REPO = "mzaiger/Stocks";      // owner/repo of the stock board. Change it if your repo has another name.
const STOCKS_INTERVAL_MINUTES = 15;        // keep equal to INTERVAL_MIN in .github/workflows/stocks.yml (allowed: 5, 10, 15, 30)

// Fires the workflow. Run by the time-driven trigger.
function triggerStocksWorkflow() {
  if (!stocksSessionOpenNow_()) {
    Logger.log("Outside Monday-Friday 4:00 AM - 8:00 PM Eastern: not dispatching.");
    return;
  }
  dispatchStocks_({ event_type: "trigger-stocks-pull" });
}

// Test button: dispatches even when the market is closed (the workflow gets force = true).
function testStocksDispatch() {
  dispatchStocks_({ event_type: "trigger-stocks-pull", client_payload: { force: true } });
}

// Run once: removes old stock triggers and creates the time-driven one.
function installStocksTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === "triggerStocksWorkflow"; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger("triggerStocksWorkflow").timeBased().everyMinutes(STOCKS_INTERVAL_MINUTES).create();
  Logger.log("Trigger installed: every " + STOCKS_INTERVAL_MINUTES + " minutes.");
}

function stocksSessionOpenNow_() {
  const p = Utilities.formatDate(new Date(), "America/New_York", "u-HH-mm").split("-").map(Number);   // u: 1 = Monday ... 7 = Sunday
  const minutes = p[1] * 60 + p[2];
  return p[0] <= 5 && minutes >= 4 * 60 && minutes < 20 * 60;     // market holidays are skipped by stocks.py itself
}

function dispatchStocks_(body) {
  const token = PropertiesService.getScriptProperties().getProperty("GITHUB_TOKEN");
  const res = UrlFetchApp.fetch("https://api.github.com/repos/" + STOCKS_REPO + "/dispatches", {
    method: "POST",
    headers: {
      "Authorization": "Bearer " + token,
      "Accept": "application/vnd.github.v3+json",
      "User-Agent": "Google-Apps-Script-Automation"
    },
    contentType: "application/json",
    payload: JSON.stringify(body),
    muteHttpExceptions: true
  });
  const code = res.getResponseCode();
  if (code === 204) {
    Logger.log("Stocks workflow dispatched.");
  } else {
    Logger.log("Dispatch failed, HTTP " + code + ": " + res.getContentText());   // 401/403 = token, 404 = repo name or token access
  }
}
