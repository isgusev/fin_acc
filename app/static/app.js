/* Учёт личных финансов — поведение интерфейса (без inline-скриптов, CSP script-src 'self') */
(function () {
  "use strict";

  function qsa(root, sel) {
    return Array.prototype.slice.call(root.querySelectorAll(sel));
  }

  function setShown(el, shown) {
    el.hidden = !shown;
    // Скрытые поля не отправляются и не проверяются браузером
    qsa(el, "input, select, textarea").forEach(function (input) {
      if (input.dataset.keepDisabled) return;
      if (!shown) {
        if (!input.disabled) input.dataset.autoDisabled = "1";
        input.disabled = true;
      } else if (input.dataset.autoDisabled) {
        delete input.dataset.autoDisabled;
        input.disabled = false;
      }
    });
  }

  function currentType(form) {
    var checked = form.querySelector("[data-op-type]:checked");
    return checked ? checked.value : "";
  }

  function applyShowFor(form, type) {
    qsa(form, "[data-show-for]").forEach(function (el) {
      var types = el.getAttribute("data-show-for").split(/\s+/);
      var shown = types.indexOf(type) !== -1;
      if (!el.querySelector("input, select, textarea")) {
        el.hidden = !shown;
      } else {
        setShown(el, shown);
      }
    });
  }

  /* ---------------------------------------------------------------- подтверждение */

  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute && form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) {
      e.preventDefault();
    }
  });

  /* ---------------------------------------------------------------- общие элементы */

  function initCommon() {
    qsa(document, "[data-dismiss]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var box = btn.closest("[data-dismissible]");
        if (box) box.remove();
      });
    });

    var toggle = document.querySelector("[data-nav-toggle]");
    var nav = document.getElementById("main-nav");
    if (toggle && nav) {
      toggle.addEventListener("click", function () {
        var open = nav.classList.toggle("open");
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
    }

    qsa(document, "[data-select-on-click]").forEach(function (el) {
      el.addEventListener("click", function () {
        var range = document.createRange();
        range.selectNodeContents(el);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      });
    });

    // «Дата до» по умолчанию повторяет «Дату от»
    qsa(document, "[data-copy-to]").forEach(function (src) {
      var dst = document.getElementById(src.getAttribute("data-copy-to"));
      if (!dst) return;
      var last = src.value;
      src.addEventListener("change", function () {
        if (!dst.value || dst.value === last || dst.value < src.value) {
          dst.value = src.value;
        }
        last = src.value;
      });
    });
  }

  /* ---------------------------------------------------------------- форма операции */

  function initOperationForm(form) {
    var accountSelect = form.querySelector("select[name=account_id]");
    var accountLabel = accountSelect ? form.querySelector("label[for=" + accountSelect.id + "]") : null;
    var planSelect = form.querySelector("[data-plan-select]");
    var allPlanOptions = planSelect ? qsa(planSelect, "option").map(function (o) { return o.cloneNode(true); }) : [];
    var nameInput = form.querySelector("input[name=name]");

    function rebuildPlans(type) {
      if (!planSelect) return;
      var selected = planSelect.value;
      planSelect.innerHTML = "";
      allPlanOptions.forEach(function (o) {
        var t = o.getAttribute("data-type");
        if (!o.value || t === type) {
          var copy = o.cloneNode(true);
          copy.selected = copy.value === selected;
          planSelect.appendChild(copy);
        }
      });
      if (planSelect.value !== selected) planSelect.value = "";
      planSelect.required = type === "income";
      var empty = planSelect.querySelector("option[value='']");
      if (empty) empty.textContent = type === "income" ? "— выберите план дохода —" : "— без плана —";
    }

    function update() {
      var type = currentType(form);
      applyShowFor(form, type);
      if (accountLabel) {
        var text = type === "transfer" ? accountSelect.dataset.labelTransfer : accountSelect.dataset.labelDefault;
        accountLabel.firstChild.nodeValue = text + " ";
      }
      if (nameInput) nameInput.required = type === "expense";
      rebuildPlans(type);
      syncCategory(type);
    }

    // Перевод: если выбранный счёт уже стоит во втором списке, второй сбрасывается
    var targetSelect = form.querySelector("select[name=target_account_id]");

    function resetPairIfSame(changed, other) {
      if (currentType(form) !== "transfer" || !changed || !other) return;
      if (changed.value && changed.value === other.value) other.value = "";
    }

    // Категория обязательна для расхода со счёта «Текущий»
    var categorySelect = form.querySelector("select[name=category_id]");
    var categoryReq = form.querySelector("[data-category-req]");

    function syncCategory(type) {
      if (!categorySelect || !accountSelect) return;
      var opt = accountSelect.options[accountSelect.selectedIndex];
      var required = type === "expense" && !!opt && opt.getAttribute("data-type-code") === "current";
      categorySelect.required = required;
      if (categoryReq) categoryReq.hidden = !required;
    }

    qsa(form, "[data-op-type]").forEach(function (r) { r.addEventListener("change", update); });
    if (accountSelect) accountSelect.addEventListener("change", function () {
      resetPairIfSame(accountSelect, targetSelect);
      update();
    });
    if (targetSelect) targetSelect.addEventListener("change", function () {
      resetPairIfSame(targetSelect, accountSelect);
      update();
    });
    update();
  }

  /* ---------------------------------------------------------------- форма счёта */

  function parseMoney(v) {
    var n = parseFloat(String(v || "").replace(/[\s\u00a0]/g, "").replace(",", "."));
    return isNaN(n) ? null : n;
  }

  function formatMoney(n) {
    return n.toFixed(2).replace(".", ",");
  }

  function initAccountForm(form) {
    var typeSelect = form.querySelector("[data-account-type]");
    var targetField = form.querySelector("[data-target-field]");
    var targetInput = form.querySelector("[data-target-input]");
    var replenish = form.querySelector("[data-replenish-input]");
    var period = form.querySelector("select[name=replenish_period]");
    var months = form.querySelector("input[name=months_to_goal]");
    var fundCode = form.getAttribute("data-fund-code");
    var periodMonths = {};
    try { periodMonths = JSON.parse(form.getAttribute("data-period-months") || "{}"); } catch (e) { periodMonths = {}; }

    function update() {
      var opt = typeSelect ? typeSelect.options[typeSelect.selectedIndex] : null;
      var isFund = !!opt && opt.getAttribute("data-type-code") === fundCode;
      if (targetField) setShown(targetField, isFund);
      var target = isFund && targetInput ? parseMoney(targetInput.value) : null;
      if (!replenish) return;
      // при заданной целевой сумме сумму пополнения считает сервер — здесь только превью
      replenish.readOnly = target !== null;
      if (target === null) return;
      var m = months ? parseInt(months.value, 10) : NaN;
      var pm = period ? periodMonths[period.value] : undefined;
      if (target > 0 && m > 0 && pm) {
        replenish.value = formatMoney(Math.round(target / (m / pm) * 100) / 100);
      } else {
        replenish.value = "";
      }
    }

    [typeSelect, targetInput, period, months].forEach(function (el) {
      if (!el) return;
      el.addEventListener("input", update);
      el.addEventListener("change", update);
    });
    update();
  }

  /* ---------------------------------------------------------------- форма планирования */

  function initPlanForm(form) {
    var kindSelect = form.querySelector("[data-kind-select]");
    var taxable = form.querySelector("[data-taxable]");
    var rate = form.querySelector("[data-rate-input]");
    var autoHint = form.querySelector("[data-auto-hint]");
    var salaryHint = form.querySelector("[data-salary-hint]");

    function selectedKind() {
      if (!kindSelect) return null;
      return kindSelect.options[kindSelect.selectedIndex] || null;
    }

    // код вида дохода при открытии формы: у сохранённой премии галочку не трогаем
    var lastCode = (function () {
      var o = selectedKind();
      return o ? o.getAttribute("data-code") : "";
    })();

    function update() {
      var type = currentType(form);
      applyShowFor(form, type);
      if (type !== "income") {
        if (salaryHint) salaryHint.hidden = true;
        return;
      }
      var opt = selectedKind();
      var code = opt ? opt.getAttribute("data-code") : "";
      var auto = opt ? opt.getAttribute("data-auto") === "1" : false;
      var isSalary = code === "salary";

      if (taxable) {
        if (isSalary) {
          taxable.checked = true;
          taxable.disabled = true;
          taxable.dataset.keepDisabled = "1";
        } else {
          taxable.disabled = false;
          delete taxable.dataset.keepDisabled;
          // премия облагается НДФЛ — при выборе вида «Премия» отмечаем по умолчанию
          if (code === "bonus" && lastCode !== "bonus") taxable.checked = true;
        }
      }
      lastCode = code;
      if (salaryHint) salaryHint.hidden = !isSalary;

      if (rate) {
        var rateOff = auto || (taxable && !taxable.checked);
        rate.disabled = rateOff;
        if (rateOff) rate.dataset.keepDisabled = "1"; else delete rate.dataset.keepDisabled;
        if (autoHint) autoHint.hidden = !auto;
      }
    }

    qsa(form, "[data-op-type]").forEach(function (r) { r.addEventListener("change", update); });
    if (kindSelect) kindSelect.addEventListener("change", update);
    if (taxable) taxable.addEventListener("change", update);
    update();
  }

  /* ---------------------------------------------------------------- импорт выписки */

  function initImportForm(form) {
    var counter = form.querySelector("[data-rows-left]");
    var submit = form.querySelector("button[type=submit]");

    function refresh() {
      var left = qsa(form, "[data-import-row]").length;
      if (counter) counter.textContent = "Строк к сохранению: " + left;
      if (submit) submit.disabled = left === 0;
    }

    form.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-remove-row]");
      if (!btn) return;
      var row = btn.closest("[data-import-row]");
      if (row) row.remove();
      refresh();
    });
    refresh();
  }

  document.addEventListener("DOMContentLoaded", function () {
    initCommon();
    qsa(document, "[data-op-form]").forEach(initOperationForm);
    qsa(document, "[data-plan-form]").forEach(initPlanForm);
    qsa(document, "[data-account-form]").forEach(initAccountForm);
    qsa(document, "[data-import-form]").forEach(initImportForm);
  });
})();
