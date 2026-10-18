/* Burger Quest 本地界面前端逻辑（无依赖，CSP script-src 'self'）。
   安全边界：所有服务端文本一律 textContent 渲染（不用 innerHTML 拼用户/服务端
   文本），从根上防 XSS；Token 只存在于服务端，前端从不读取/发送。 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

  // ---------- 工具 ----------
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function show(id, visible) { $(id).hidden = !visible; }

  var STATE_TEXT = {
    success: "成功", partial: "部分数据", no_result: "无结果",
    permission: "权限不足", connection: "连接失败", rate_limited: "服务限流", loading: "加载中"
  };

  function stateBanner(state, detail) {
    var banner = el("div", "state-banner");
    banner.setAttribute("role", "status");
    banner.dataset.state = state;
    banner.appendChild(el("span", "icon", state === "success" ? "✅" :
      state === "partial" ? "🧩" : state === "no_result" ? "🫥" :
      state === "permission" ? "🔒" : state === "rate_limited" ? "⏳" : "📡"));
    var body = el("div", "body");
    body.appendChild(el("b", null, STATE_TEXT[state] || state));
    if (detail) body.appendChild(el("div", "detail", detail));
    banner.appendChild(body);
    return banner;
  }

  // ---------- 连接状态 ----------
  function refreshStatus() {
    $("conn-text").textContent = "正在检测连接…";
    $("conn-dot").dataset.state = "unknown";
    fetch("/api/status").then(function (r) { return r.json(); }).then(function (data) {
      renderStatus(data);
    }).catch(function () {
      $("conn-text").textContent = "无法连接本地服务";
      $("conn-dot").dataset.state = "err";
    });
  }

  function renderStatus(data) {
    var levels = $("levels");
    clear(levels);
    var allOk = true, anyFail = false;
    (data.levels || []).forEach(function (lv) {
      var li = el("li");
      li.dataset.ok = lv.ok === null ? "none" : String(!!lv.ok);
      li.appendChild(el("span", "lv", lv.level + " " + lv.name));
      li.appendChild(el("span", null, lv.ok === null ? "未执行" : (lv.ok ? "PASS" : "FAIL")));
      if (lv.detail) li.appendChild(el("span", "detail", lv.detail));
      levels.appendChild(li);
      if (lv.ok === false) { allOk = false; anyFail = true; }
    });
    var dot = $("conn-dot");
    if (allOk && !anyFail) { dot.dataset.state = "ok"; $("conn-text").textContent = data.token_present ? "真实 MCP 就绪" : "服务可访问（未配置 Token，真实查询将明确报错）"; }
    else if (data.overall === "config") { dot.dataset.state = "warn"; $("conn-text").textContent = "配置待完成（见下方状态）"; }
    else { dot.dataset.state = "err"; $("conn-text").textContent = "连接异常（见下方状态）"; }
    if (!data.token_present) {
      var hint = $("status-hint");
      hint.hidden = false;
      hint.textContent = "未检测到 MCD_MCP_TOKEN：真实查询会返回「权限不足/配置」提示，不会自动切换演示数据。";
    } else {
      $("status-hint").hidden = true;
    }
  }

  // ---------- 任务 ----------
  function runQuest(text, storeCode) {
    var panel = $("result-panel"), body = $("result-body");
    show("result-panel", true);
    clear(body);
    body.setAttribute("aria-busy", "true");
    var loading = el("p");
    loading.appendChild(el("span", "spinner"));
    loading.appendChild(document.createTextNode(" 正在通过官方水晶球求证（真实 MCP 查询）…"));
    body.appendChild(loading);
    $("btn-quest").disabled = true;

    fetch("/api/quest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, store_code: storeCode || "" })
    }).then(function (r) { return r.json(); }).then(function (data) {
      body.removeAttribute("aria-busy");
      renderResult(data);
    }).catch(function (e) {
      body.removeAttribute("aria-busy");
      clear(body);
      body.appendChild(stateBanner("connection", "本地服务无响应：" + e));
    }).then(function () { $("btn-quest").disabled = false; });
  }

  function renderResult(data) {
    var body = $("result-body");
    clear(body);
    body.appendChild(stateBanner(data.state, data.error || null));
    if (data.state !== "success" && data.state !== "partial") return;

    // 门店信息
    if (data.store) {
      var storeLine = el("p", "hint", "门店：" + (data.store.name || "（未命名）") + " · 编码 " + data.store.code);
      body.appendChild(storeLine);
    }
    if (data.store_candidates && data.store_candidates.length > 1) {
      var list = el("ul", "store-list");
      data.store_candidates.forEach(function (s) {
        var li = el("li");
        li.appendChild(el("div", null, s.name || "（未命名门店）"));
        li.appendChild(el("div", "code", "编码：" + s.code));
        if (s.address) li.appendChild(el("div", "addr", s.address));
        list.appendChild(li);
      });
      body.appendChild(el("p", "hint", "找到多家附近门店，请指定其一（把编码填入「门店编码」后重试）："));
      body.appendChild(list);
    }
    // 任务名与故事（真实数据 + RPG 包装分离）
    if (data.quest_name) body.appendChild(el("h3", null, data.quest_name));
    if (data.story) body.appendChild(el("p", null, data.story));

    // 方案
    (data.options || []).forEach(function (o, i) {
      var card = el("article", "plan");
      var head = el("header");
      head.appendChild(el("h4", null, o.title));
      if (i === data.chosen_index) {
        var star = el("span", "badge", "推荐");
        head.appendChild(star);
      }
      if (o.budget) {
        head.appendChild(el("span", "badge", o.within_budget === true ? "预算内" : o.within_budget === false ? "超预算" : "预算未设"));
      }
      head.appendChild(el("span", "total", o.total));
      card.appendChild(head);

      var items = el("ul", "items");
      (o.items || []).forEach(function (it) {
        var li = el("li");
        li.appendChild(el("span", "name", it.name + (it.qty > 1 ? " ×" + it.qty : "")));
        li.appendChild(el("span", "price", it.price));
        li.appendChild(el("span", "code", "商品编码 " + it.code));
        items.appendChild(li);
      });
      card.appendChild(items);

      var pl = el("div", "price-lines");
      pl.appendChild(row("原价合计", o.original));
      if (o.discount) pl.appendChild(row("已核实优惠", "-" + o.discount));
      if (o.delivery_fee) pl.appendChild(row("配送费", o.delivery_fee));
      var totalRow = row("应付总价（calculate-price 权威）", o.total);
      totalRow.classList.add("total");
      pl.appendChild(totalRow);
      if (o.remaining !== null && o.remaining !== undefined) {
        pl.appendChild(row(o.remaining.startsWith("-") ? "超出预算" : "剩余预算", o.remaining.replace("-", "")));
      }
      card.appendChild(pl);

      if (o.reasons && o.reasons.length) {
        var ul = el("ul", "reasons");
        o.reasons.forEach(function (r) { ul.appendChild(el("li", null, "✅ " + r)); });
        card.appendChild(ul);
      }
      if (o.unmet && o.unmet.length) {
        var ul2 = el("ul", "unmet");
        o.unmet.forEach(function (u) { ul2.appendChild(el("li", null, "⚠️ " + u)); });
        card.appendChild(ul2);
      }
      body.appendChild(card);
    });

    // 警告（含排除/缺失/失败明细——不掩盖）
    if (data.warnings && data.warnings.length) {
      var warns = el("ul", "warnings");
      data.warnings.forEach(function (w) { warns.appendChild(el("li", null, w)); });
      body.appendChild(stateBannerHeader(warns));
    }
    // 数据来源与调用审计
    if (data.data_sources) {
      var src = el("div", "sources");
      src.appendChild(el("div", null, "数据来源（真实 MCP 查询，非演示数据）："));
      Object.keys(data.data_sources).forEach(function (k) {
        src.appendChild(el("div", null, k + ": " + data.data_sources[k]));
      });
      body.appendChild(src);
    }
    if (data.mcp_calls_summary && data.mcp_calls_summary.total) {
      body.appendChild(el("p", "hint", "MCP 调用：真实请求 " + data.mcp_calls_summary.total + " 次（失败 " + data.mcp_calls_summary.failed + " 次）"));
    }
    // 本地进度
    if (data.progress_lines && data.progress_lines.length) {
      (data.progress_lines || []).forEach(function (line) { body.appendChild(el("p", "hint", line)); });
    }
    // 分享卡（服务端转义后完整文档，新窗打开）
    var cardBtn = el("button", "btn btn-small", "查看/保存分享卡");
    cardBtn.type = "button";
    cardBtn.addEventListener("click", function () { openCard(currentText(), currentStore()); });
    body.appendChild(el("p", "hint", "分享卡经服务端转义生成；含 NON-OFFICIAL 声明与 LIVE/DEMO 徽章。"));
    body.appendChild(cardBtn);
  }

  function row(label, value) {
    var r = el("div", "row");
    r.appendChild(el("span", null, label));
    r.appendChild(el("span", null, value));
    return r;
  }
  function stateBannerHeader(node) {
    var wrap = el("div");
    wrap.appendChild(el("p", "hint", "提示（真实链路明细）："));
    wrap.appendChild(node);
    return wrap;
  }

  // ---------- 门店查询 ----------
  function queryStores() {
    var text = $("text").value.trim();
    if (!text) { alert("请先在需求框输入位置信息（如：在深圳市南山区科技园）"); return; }
    var panel = $("stores-panel"), body = $("stores-body");
    show("stores-panel", true);
    clear(body);
    body.setAttribute("aria-busy", "true");
    body.appendChild(el("p", null, "正在查询附近门店…"));
    // 位置提取：取需求文本里「在X」之后的片段交给服务端解析（服务端仍走 query-nearby-stores）
    fetch("/api/stores", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: text })
    }).then(function (r) { return r.json(); }).then(function (data) {
      body.removeAttribute("aria-busy");
      clear(body);
      if (data.state !== "success") {
        body.appendChild(stateBanner(data.state, data.error || data.hint || "未返回门店"));
        return;
      }
      var list = el("ul", "store-list");
      data.stores.forEach(function (s) {
        var li = el("li");
        li.appendChild(el("div", null, s.name || "（未命名门店）"));
        li.appendChild(el("div", "code", "编码：" + s.code));
        if (s.address) li.appendChild(el("div", "addr", s.address));
        var btn = el("button", "btn btn-small", "用这家");
        btn.type = "button";
        btn.addEventListener("click", function () { $("store_code").value = s.code; });
        li.appendChild(btn);
        list.appendChild(li);
      });
      body.appendChild(list);
    }).catch(function (e) {
      body.removeAttribute("aria-busy");
      clear(body);
      body.appendChild(stateBanner("connection", "本地服务无响应：" + e));
    });
  }

  // ---------- 分享卡 ----------
  function openCard(text, storeCode) {
    fetch("/api/card", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, store_code: storeCode || "" })
    }).then(function (r) {
      if (!r.ok) { return r.json().then(function (d) { alert((STATE_TEXT[d.state] || "错误") + "：" + (d.error || "无法生成分享卡")); }); }
      return r.text();
    }).then(function (html) {
      if (!html) return;
      var w = window.open("", "_blank");
      if (w) { w.document.open(); w.document.write(html); w.document.close(); }
      else alert("浏览器拦截了新窗口，请允许弹出窗口后重试");
    }).catch(function (e) { alert("生成分享卡失败：" + e); });
  }

  // ---------- 进度 ----------
  function showProgress() {
    var panel = $("progress-panel"), body = $("progress-body");
    show("progress-panel", true);
    clear(body);
    body.setAttribute("aria-busy", "true");
    body.appendChild(el("p", null, "正在读取本地进度…"));
    fetch("/api/progress").then(function (r) { return r.json(); }).then(function (data) {
      body.removeAttribute("aria-busy");
      clear(body);
      if (data.enabled) {
        body.appendChild(el("p", null, "LV." + data.level + " " + data.title + " · 累计经验 " + data.xp + " · 成就 " + data.unlocked + "/" + data.total));
        (data.chapters || []).forEach(function (c) { body.appendChild(el("p", null, c)); });
      } else {
        body.appendChild(el("p", null, data.note || "未启用本地进度"));
      }
    }).catch(function () {
      body.removeAttribute("aria-busy");
      clear(body);
      body.appendChild(stateBanner("connection", "无法读取本地进度"));
    });
  }

  // ---------- 当前输入（供分享卡复用） ----------
  function currentText() { return $("text").value.trim(); }
  function currentStore() { return $("store_code").value.trim(); }

  // ---------- 事件绑定 ----------
  document.addEventListener("DOMContentLoaded", function () {
    refreshStatus();
    $("btn-recheck").addEventListener("click", refreshStatus);
    $("quest-form").addEventListener("submit", function (e) {
      e.preventDefault();
      var text = currentText();
      if (!text) { $("text").focus(); return; }
      runQuest(text, currentStore());
    });
    $("btn-stores").addEventListener("click", queryStores);
    $("btn-progress").addEventListener("click", showProgress);
    // 常用句式快捷填充（2026-10-18）：点击填入需求框，用户可继续修改再提交
    document.querySelectorAll("[data-fill]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var t = $("text");
        if (t) { t.value = btn.getAttribute("data-fill"); t.focus(); }
      });
    });
  });
})();
