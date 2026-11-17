(function () {
  "use strict";

  var panel = document.querySelector("[data-plan-status]");
  if (!panel) return;

  var url = panel.getAttribute("data-status-url");
  if (!url) return;

  var timer = window.setInterval(function () {
    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (!data) return;
        if (data.status === "paid" || data.status === "failed" || data.status === "abandoned") {
          window.clearInterval(timer);
          window.location.reload();
        }
      })
      .catch(function (error) {
        console.warn("Plan payment status check failed", error);
      });
  }, 3000);
})();
