(function () {
  "use strict";

  var script = document.currentScript;
  var statusUrl = script ? script.getAttribute("data-status-url") : null;
  if (!statusUrl) {
    return;
  }

  var timer = null;
  var reloaded = false;

  function setText(id, value) {
    var el = document.getElementById(id);
    if (el) {
      el.textContent = value === null || value === undefined ? "-" : String(value);
    }
  }

  function updateStyles(styles) {
    var byName = {};
    (styles || []).forEach(function (item) {
      byName[item.name] = item;
    });
    var rows = document.querySelectorAll("#style-table tbody tr[data-name]");
    rows.forEach(function (row) {
      var item = byName[row.getAttribute("data-name")];
      if (!item) {
        return;
      }
      var statusCell = row.querySelector(".style-status");
      var errorCell = row.querySelector(".style-error");
      var cacheCell = row.querySelector(".style-cache");
      if (statusCell) {
        statusCell.textContent = item.status;
      }
      if (errorCell) {
        errorCell.textContent = item.error_message || "";
      }
      if (cacheCell) {
        cacheCell.textContent = item.cache_hit ? "yes" : "no";
      }
    });
  }

  function poll() {
    fetch(statusUrl)
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        setText("job-status", data.status);
        setText("job-stage", data.stage);
        setText("styles-total", data.styles_total);
        setText("styles-done", data.styles_done);
        setText("styles-failed", data.styles_failed);
        updateStyles(data.styles);
        if (data.terminal) {
          if (timer) {
            clearInterval(timer);
            timer = null;
          }
          if (!reloaded) {
            reloaded = true;
            location.reload();
          }
        }
      })
      .catch(function () {});
  }

  timer = setInterval(poll, 3000);
})();
