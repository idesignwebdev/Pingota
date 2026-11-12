/* Portal status poller.
   Polls a small JSON endpoint instead of holding a websocket, so it survives
   flaky mobile networks and needs no extra server process. When a check fails,
   it stops and offers a button instead of spinning forever. */
(function () {
  "use strict";

  var root = document.getElementById("status-root");
  if (!root) return;

  var url = root.getAttribute("data-state-url");
  var label = document.getElementById("state-label");
  var note = root.querySelector("[data-poll-note]");
  var retry = root.querySelector("[data-poll-retry]");
  var attempts = 0;
  var maxAttempts = 60; // about two minutes at 2s
  var timer = null;

  function describe(state) {
    if (!state.grant) {
      if (state.payment && state.payment.status === "failed") return "Payment failed";
      return "Waiting for confirmation";
    }
    if (state.grant.state === "active" && state.grant.seconds_left > 0) return "Online";
    if (state.grant.state === "expired" || state.grant.seconds_left <= 0) return "Ended";
    return "Waiting for the router";
  }

  function showRetry(message) {
    if (note) note.textContent = message;
    if (retry) retry.hidden = false;
  }

  function schedule() {
    if (attempts < maxAttempts) {
      timer = window.setTimeout(tick, 2000);
    } else {
      showRetry("Still waiting. Check again, or ask the operator for help.");
    }
  }

  function tick() {
    if (timer) {
      window.clearTimeout(timer);
      timer = null;
    }
    attempts += 1;
    if (retry) retry.hidden = true;
    if (note) note.textContent = "Checking the payment...";

    fetch(url, { headers: { "Accept": "application/json" }, cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("status request failed");
        return response.json();
      })
      .then(function (state) {
        if (!state) throw new Error("empty status response");

        if (label) label.textContent = describe(state);

        if (state.grant && state.grant.state === "active" && state.grant.seconds_left > 0) {
          if (note) note.textContent = "Access is active. Reloading this page.";
          window.location.reload();
          return;
        }

        if (state.payment && state.payment.status === "failed") {
          if (note) note.textContent = "The payment did not complete. Reloading this page.";
          window.location.reload();
          return;
        }

        if (note) note.textContent = "Waiting for the router to confirm your pass.";
        schedule();
      })
      .catch(function () {
        showRetry("We could not check the status. Your payment is not lost. Check again, or ask the operator for help.");
      });
  }

  if (retry) {
    retry.addEventListener("click", function () {
      attempts = 0;
      tick();
    });
  }

  timer = window.setTimeout(tick, 1500);
})();
