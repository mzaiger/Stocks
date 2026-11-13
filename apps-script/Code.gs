// Stock Board: Google Apps Script timer for the "Update stocks" GitHub workflow.
// Paste this whole file into your Apps Script project (script.google.com), save it, set the GITHUB_TOKEN
// script property, and attach a time-driven trigger (every 15 minutes) to triggerStocks.
// See README, "Timing (Google Apps Script)".

const STOCKS_REPO = "mzaiger/Stocks";   // your GitHub repo: owner/repo

// Your 15-minute trigger runs this one.
function triggerStocks() {
  const p = Utilities.formatDate(new Date(), "America/New_York", "u-HH-mm").split("-").map(Number);   // u: 1 = Monday ... 7 = Sunday
  const minutes = p[1] * 60 + p[2];
  const open = p[0] <= 5 && minutes >= 4 * 60 && minutes < 20 * 60;   // Mon-Fri, 4:00 AM to 8:00 PM Eastern (market holidays are skipped by stocks.py)
  if (!open) {
    Logger.log("Market session closed: not dispatching.");
    return;
  }
  sendStocksDispatch_({ event_type: "trigger-stocks-pull" });
}

// Optional: run this by hand once to test. It works even when the market is closed (the workflow gets force = true).
function testStocks() {
  sendStocksDispatch_({ event_type: "trigger-stocks-pull", client_payload: { force: true } });
}

function sendStocksDispatch_(body) {
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
  Logger.log(code === 204 ? "Stocks workflow dispatched." : "Dispatch failed, HTTP " + code + ": " + res.getContentText());   // 401/403 = token, 404 = repo name or token access
}
