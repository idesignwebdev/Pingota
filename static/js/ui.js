(function () {
  "use strict";

  // Prevent a second submit and show a named working state.
  document.querySelectorAll("form[data-busy]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (form.dataset.confirm && form.dataset.confirmed !== "true") return;
      var button = event.submitter || form.querySelector('button[type="submit"], input[type="submit"]');
      if (!button || button.disabled) return;
      button.setAttribute("aria-busy", "true");
      button.dataset.originalText = button.textContent;
      button.textContent = form.dataset.busy || button.dataset.busy || "Working";
      window.setTimeout(function () {
        button.disabled = true;
      }, 0);
    });
  });

  // The header nav uses a native details element for the mobile menu.
  // Keep it expanded at desktop widths and collapsed on small screens.
  var disclosures = document.querySelectorAll(".nav-disclosure");
  if (disclosures.length && window.matchMedia) {
    var wide = window.matchMedia("(min-width: 900px)");
    var syncNav = function () {
      disclosures.forEach(function (item) {
        item.open = wide.matches;
      });
    };
    syncNav();
    if (wide.addEventListener) {
      wide.addEventListener("change", syncNav);
    } else if (wide.addListener) {
      wide.addListener(syncNav);
    }
  }

  // Native dialog for destructive actions.
  var dialog = document.getElementById("confirm-dialog");
  if (dialog) {
    var message = dialog.querySelector("[data-confirm-message]");
    var accept = dialog.querySelector("[data-confirm-accept]");
    var cancel = dialog.querySelector("[data-confirm-cancel]");
    var pendingForm = null;

    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (form.dataset.confirmed === "true") return;
        event.preventDefault();
        pendingForm = form;
        if (message) message.textContent = form.dataset.confirm;
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else if (window.confirm(form.dataset.confirm)) {
          form.dataset.confirmed = "true";
          form.submit();
        }
      });
    });

    if (accept) {
      accept.addEventListener("click", function () {
        if (!pendingForm) return;
        pendingForm.dataset.confirmed = "true";
        if (typeof pendingForm.requestSubmit === "function") {
          pendingForm.requestSubmit();
        } else {
          pendingForm.submit();
        }
        dialog.close();
      });
    }

    if (cancel) {
      cancel.addEventListener("click", function () {
        dialog.close();
      });
    }

    dialog.addEventListener("close", function () {
      pendingForm = null;
    });
  }

  // Keep in-page navigation in sync with the section in view.
  var scrollNav = document.querySelector("[data-scrollspy]");
  if (scrollNav && "IntersectionObserver" in window) {
    var links = Array.prototype.slice.call(scrollNav.querySelectorAll('a[href^="#"]'));
    var sections = links
      .map(function (link) {
        return document.querySelector(link.getAttribute("href"));
      })
      .filter(Boolean);

    var setCurrent = function (id) {
      links.forEach(function (link) {
        if (link.getAttribute("href") === "#" + id) {
          link.setAttribute("aria-current", "location");
        } else {
          link.removeAttribute("aria-current");
        }
      });
    };

    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) setCurrent(entry.target.id);
        });
      },
      { rootMargin: "-20% 0px -70% 0px" }
    );

    sections.forEach(function (section) {
      observer.observe(section);
    });
  }
})();
