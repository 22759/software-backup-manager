/* 软件备份管理器 - 前端逻辑 */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  let api = null;
  let APPS = [];          // 全部应用
  let filter = "all";     // all | winget | upd
  let query = "";
  let sortBy = "name";       // name | name_desc | date_desc | date_asc | version_desc | size_desc
  let dateRange = "all";     // all | 7 | 30 | 90 | 180 | 365 | older | unknown
  let onlyBackup = false;    // 只看已下载过安装包的
  let BACKUP_IDX = {};       // {winget_id: {path, name, version, installer_type, exists}}
  let pollTimer = null;
  let selSaveTimer = null;
  let settings = {
    theme: "galaxy",
    themeMode: "auto",     // dark | light | auto（跟随系统）
    bgImagePath: "",
    bgOpacity: 100,
    panelAlpha: 100,
    desktopTransparent: false,
    flat: false,
    backupDir: "",
    installDir: "",
    installerType: "auto",   // auto(优先EXE) | msi | zip | all
    sortBy: "name",
    dateRange: "all",
  };

  // ---------- 主题配色 ----------
  const THEMES = {
    galaxy:   { name: "星河", a1: "#6366f1", a2: "#8b5cf6", a3: "#22d3ee", a4: "#ec4899" },
    aurora:   { name: "极光", a1: "#10b981", a2: "#14b8a6", a3: "#34d399", a4: "#22d3ee" },
    sunset:   { name: "日落", a1: "#fb923c", a2: "#f43f5e", a3: "#f59e0b", a4: "#ec4899" },
    ocean:    { name: "海洋", a1: "#3b82f6", a2: "#06b6d4", a3: "#60a5fa", a4: "#22d3ee" },
    sakura:   { name: "樱粉", a1: "#ec4899", a2: "#d946ef", a3: "#f472b6", a4: "#a78bfa" },
    mint:     { name: "薄荷", a1: "#14b8a6", a2: "#34d399", a3: "#2dd4bf", a4: "#10b981" },
    graphite: { name: "石墨", a1: "#64748b", a2: "#475569", a3: "#94a3b8", a4: "#10b981" },
  };

  function applyTheme(name) {
    const t = THEMES[name] || THEMES.galaxy;
    const r = document.documentElement.style;
    r.setProperty("--accent1", t.a1);
    r.setProperty("--accent2", t.a2);
    r.setProperty("--accent3", t.a3);
    r.setProperty("--accent4", t.a4);
    document.querySelectorAll(".swatch").forEach(s =>
      s.classList.toggle("active", s.dataset.theme === name));
    settings.theme = name;
    persistSettings();
  }

  // ---------- 外观模式：深色 / 浅色 / 跟随系统 ----------
  // 注意：WebView2 里 prefers-color-scheme 常常固定返回 light，跟不上 Windows 设置，
  // 所以「跟随系统」改为由后端读注册表 AppsUseLightTheme，matchMedia 只作兜底。
  const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  let sysTheme = null;      // "dark" | "light" | null(未探测)
  let sysThemeTimer = null;

  function fallbackSysTheme() {
    return (mq && mq.matches) ? "dark" : "light";
  }

  function effectiveMode(mode) {
    if (mode === "auto") return sysTheme || fallbackSysTheme();
    return mode;
  }

  function applyMode(mode) {
    settings.themeMode = mode;
    const eff = effectiveMode(mode);
    const root = document.documentElement;
    root.classList.toggle("light-mode", eff === "light");
    root.classList.toggle("dark-mode", eff === "dark");
    root.style.colorScheme = eff;   // 原生控件（下拉框/滑杆/滚动条）跟随
    document.querySelectorAll("#modeSeg button").forEach(b =>
      b.classList.toggle("active", b.dataset.mode === mode));
    // 「跟随系统」按钮直接标出它当前解析成了什么
    const autoBtn = document.querySelector('#modeSeg button[data-mode="auto"]');
    if (autoBtn) {
      const eff = effectiveMode("auto");
      autoBtn.textContent = "🖥 跟随系统（" + (eff === "dark" ? "深色" : "浅色") + "）";
    }
    updateSysThemeTip();
    persistSettings();
  }

  // 从后端读取 Windows 当前主题
  async function syncSystemTheme() {
    let t = null;
    try {
      if (api && api.get_system_theme) t = await api.get_system_theme();
    } catch (e) { t = null; }
    if (t !== "dark" && t !== "light") t = fallbackSysTheme();
    const changed = (t !== sysTheme);
    sysTheme = t;
    if (changed && settings.themeMode === "auto") applyMode("auto");
    else updateSysThemeTip();
    return sysTheme;
  }

  function updateSysThemeTip() {
    const el = $("#sysThemeTip");
    if (!el) return;
    if (!sysTheme) { el.textContent = ""; return; }
    el.textContent = `（当前系统：${sysTheme === "dark" ? "深色 🌙" : "浅色 ☀️"}）`;
  }

  function startSysThemeWatch() {
    if (sysThemeTimer) return;
    sysThemeTimer = setInterval(() => {
      if (settings.themeMode === "auto") syncSystemTheme();
    }, 2500);
  }

  function onSystemThemeChange() {
    if (settings.themeMode === "auto") applyMode("auto");
  }
  if (mq) {
    if (mq.addEventListener) mq.addEventListener("change", onSystemThemeChange);
    else if (mq.addListener) mq.addListener(onSystemThemeChange);
  }

  function applyBgImage(dataUri, opacityPct) {
    const el = $("#bgImg");
    el.style.backgroundImage = dataUri ? `url("${dataUri}")` : "";
    document.documentElement.style.setProperty("--bgimg", (opacityPct / 100).toFixed(3));
    $("#bgOpacity").value = opacityPct;
    $("#bgOpacityVal").textContent = opacityPct + "%";
  }

  function persistSettings() {
    if (api && api.save_settings) {
      api.save_settings({
        theme: settings.theme,
        themeMode: settings.themeMode,
        bgImagePath: settings.bgImagePath || "",
        bgOpacity: settings.bgOpacity ?? 100,
        panelAlpha: settings.panelAlpha ?? 100,
        desktopTransparent: !!settings.desktopTransparent,
        flat: !!settings.flat,
        backupDir: settings.backupDir || "",
        installDir: settings.installDir || "",
        installerType: settings.installerType || "auto",
        sortBy: settings.sortBy || "name",
        dateRange: settings.dateRange || "all",
      });
    }
  }

  // ---------- 勾选记忆 ----------
  function scheduleSaveSelection() {
    clearTimeout(selSaveTimer);
    selSaveTimer = setTimeout(async () => {
      if (!api || !api.save_selection) return;
      const ids = selectedApps().map(a => a.id);
      try { await api.save_selection(ids); } catch (e) { /* 忽略 */ }
    }, 400);
  }

  function makeDraggable(el, handle) {
    let sx = 0, sy = 0, ox = 0, oy = 0, dragging = false;
    handle.addEventListener("mousedown", (e) => {
      if (e.target.closest(".ap-close")) return;
      dragging = true;
      const r = el.getBoundingClientRect();
      el.style.left = r.left + "px";
      el.style.top = r.top + "px";
      el.style.right = "auto";
      sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top;
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      let nx = ox + (e.clientX - sx);
      let ny = oy + (e.clientY - sy);
      nx = Math.max(0, Math.min(nx, window.innerWidth - el.offsetWidth));
      ny = Math.max(0, Math.min(ny, window.innerHeight - el.offsetHeight));
      el.style.left = nx + "px";
      el.style.top = ny + "px";
    });
    window.addEventListener("mouseup", () => { dragging = false; });
  }

  function buildSwatches() {
    const row = $("#themeRow");
    Object.entries(THEMES).forEach(([key, t]) => {
      const s = document.createElement("div");
      s.className = "swatch";
      s.dataset.theme = key;
      s.style.background = `linear-gradient(135deg, ${t.a1}, ${t.a2} 55%, ${t.a4})`;
      s.innerHTML = `<span class="sw-name">${t.name}</span>`;
      s.addEventListener("click", () => applyTheme(key));
      row.appendChild(s);
    });
  }

  // ---------- 工具 ----------
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(t._t);
    t._t = setTimeout(() => (t.hidden = true), 2600);
  }

  function selectedApps() {
    return APPS.filter(a => a._sel && (a.winget || a.local));
  }

  function payload() {
    return selectedApps().map(a => ({
      name: a.name, id: a.id, version: a.version, source: a.source,
      winget: a.winget, local: a.local || null
    }));
  }

  function updateSelInfo() {
    const n = selectedApps().length;
    const dead = APPS.filter(a => a._linkDead).length;
    $("#selInfo").textContent = dead
      ? `已选 ${n} 个 · ${dead} 个链接失效`
      : `已选 ${n} 个`;
  }

  // ---------- 本地安装包链接 ----------
  function guessArgs(p) {
    const ext = (p || "").toLowerCase().split(".").pop();
    if (ext === "msi")
      return 'msiexec /i "{path}" /qn /norestart INSTALLDIR="{loc}"';
    return '"{path}" /S /D="{loc}"';
  }

  function collectLinks() {
    const o = {};
    for (const a of APPS) if (a.local && a.local.path) o[a.id] = a.local;
    return o;
  }

  async function saveLinksQuiet() {
    try { await api.save_links(collectLinks()); } catch (e) { /* 忽略 */ }
  }

  // 失效链接检测
  async function checkLinkValidity() {
    const withLocal = APPS.filter(a => a.local && a.local.path);
    if (!withLocal.length) return 0;
    let res = {};
    try { res = await api.check_links(withLocal.map(a => a.local.path)); } catch (e) { return 0; }
    let bad = 0;
    for (const a of withLocal) {
      const ok = res[a.local.path];
      a._linkDead = (ok === false);
      if (a._linkDead) bad++;
    }
    return bad;
  }

  async function linkApp(a) {
    const r = await api.pick_installer();
    if (!r || !r.path) {
      if (r && r.error) toast("选择失败：" + r.error);
      return;
    }
    a.local = { path: r.path, args: guessArgs(r.path) };
    a._linkDead = false;
    await saveLinksQuiet();
    render();
    updateSelInfo();
    toast("已链接本地安装包：" + (r.name || r.path));
  }

  async function unlinkApp(a) {
    delete a.local;
    a._linkDead = false;
    await saveLinksQuiet();
    render();
    updateSelInfo();
  }

  function dirKeyJs(s) {
    return (s || "").replace(/[^0-9A-Za-z.]+/g, "_").replace(/^_+|_+$/g, "") || "app";
  }

  async function openLinkLoc(a) {
    if (!a.local || !a.local.path) return;
    if (a._linkDead) return toast("链接已失效：找不到 " + a.local.path);
    // 打开前再确认一次文件还在（可能刚被移动/删除）
    try {
      if (api.file_exists && !(await api.file_exists(a.local.path))) {
        a._linkDead = true;
        render(); updateSelInfo();
        return toast("链接已失效：找不到 " + a.local.path);
      }
    } catch (e) { /* 忽略 */ }
    // 优先打开备份目录下的对应子文件夹（下载/导出后文件会在这里），否则打开源文件所在文件夹
    const backup = $("#backupDir").value.trim();
    const sub = backup ? (backup + "\\" + dirKeyJs(a.name)) : "";
    const srcDir = a.local.path.replace(/[\\/][^\\/]*$/, "");
    let target = srcDir;
    if (backup) {
      try { if (await api.dir_exists(sub)) target = sub; } catch (e) {}
    }
    try { await api.open_folder(target); } catch (e) {}
  }

  // ---------- 批量链接：一次选多个安装包，分别指定软件 ----------
  function normName(s) {
    return String(s || "")
      .toLowerCase()
      .replace(/\.(exe|msi|msix|msixbundle|appx|appxbundle|cab|zip|7z|tar|gz|tgz)$/i, "")
      .replace(/[\s_\-.,()\[\]]+/g, "");
  }

  function guessTarget(file) {
    const base = normName(file.name);
    const stem = String(file.name || "").replace(/\.[^.]+$/, "");
    const toks = stem.toLowerCase().split(/[^a-z0-9\u4e00-\u9fa5]+/).filter(t => t.length > 2);
    let best = null, bestScore = 0;
    for (const a of APPS) {
      const n = normName(a.name);
      if (!n) continue;
      let score = 0;
      if (base && base.indexOf(n) >= 0) score = n.length * 2;
      else if (n.indexOf(base) >= 0 && base.length >= 3) score = base.length * 2;
      else {
        const hit = toks.filter(t => n.indexOf(t) >= 0).length;
        score = hit ? hit * 2 : 0;
      }
      // 文件名以软件名首词开头（如 Notepad++ ↔ notepadplusplus.exe）
      const firstWord = String(a.name).split(/[\s(\[]/)[0].toLowerCase()
        .replace(/[^a-z0-9\u4e00-\u9fa5]/g, "");
      if (firstWord.length >= 4 && base.indexOf(firstWord) === 0) {
        score = Math.max(score, firstWord.length * 2 + 1);
      }
      if (score > bestScore) { bestScore = score; best = a; }
    }
    return bestScore > 0 ? best : null;
  }

  async function batchLink(ev) {
    let files = [];
    try { files = await api.pick_installers(); } catch (e) { files = []; }
    if (!files || !files.length) return;

    const opts = ['<option value="">（不链接 / 跳过）</option>'].concat(
      APPS.map(a => `<option value="${esc(a.id)}">${esc(a.name)}${a.winget ? "" : " ·非winget"}</option>`)
    ).join("");

    const rows = files.map((f, i) => {
      const sug = guessTarget(f);
      return `<div class="batch-row${sug ? " suggest" : ""}" data-i="${i}">
        <div class="batch-file">
          <div class="bf-name">${esc(f.name)}${sug ? '<span class="batch-tag">建议</span>' : ""}</div>
          <div class="bf-path">${esc(f.path)}</div>
        </div>
        <select data-sel="${i}">${opts}</select>
      </div>`;
    }).join("");

    const html = `
      <div class="pop-head"><span>📦 批量链接安装包</span><button class="pop-close" data-x>✕</button></div>
      <div class="pop-note">
        一共选了 <b>${files.length}</b> 个安装包。请为每个文件选择要链接到的软件，
        标有「<b>建议</b>」的是按文件名自动匹配的结果——<b>请确认后再点完成</b>，避免链接错软件。
      </div>
      <div class="batch-list">${rows}</div>
      <div class="pop-actions"><button class="btn" data-x>取消</button><button class="btn primary" data-ok>完成</button></div>`;

    const pop = showPop(html, ev.clientX, ev.clientY, true);
    pop.querySelectorAll("select[data-sel]").forEach(sel => {
      const i = +sel.dataset.sel;
      const sug = guessTarget(files[i]);
      if (sug) sel.value = sug.id;
    });
    pop.querySelector("[data-x]").onclick = hidePop;
    pop.querySelector("[data-ok]").onclick = async () => {
      const assign = {};
      pop.querySelectorAll("select[data-sel]").forEach(sel => {
        if (sel.value) assign[sel.value] = files[+sel.dataset.sel].path;
      });
      let n = 0;
      for (const a of APPS) {
        if (assign[a.id]) {
          a.local = { path: assign[a.id], args: guessArgs(assign[a.id]) };
          a._linkDead = false;
          n++;
        }
      }
      await saveLinksQuiet();
      hidePop();
      await checkLinkValidity();
      render();
      updateSelInfo();
      toast(n ? `已批量链接 ${n} 个安装包` : "没有选择任何软件");
    };
  }

  // ---------- 导出清单 ----------
  function csvCell(v) {
    const s = String(v == null ? "" : v);
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  async function exportList() {
    const sel = selectedApps();
    if (!sel.length) return toast("请先勾选要导出的软件");

    const r = await api.pick_save_file("导出软件清单");
    if (!r || !r.path) return;

    const stamp = new Date().toLocaleString("zh-CN");
    const isCsv = /\.csv$/i.test(r.path);
    let content;

    if (isCsv) {
      const rows = [["名称", "ID", "已装版本", "可更新版本", "安装日期", "安装位置",
                     "来源", "本地安装包", "已下载安装包", "安装命令"]];
      for (const a of sel) {
        const bk = BACKUP_IDX[a.id];
        rows.push([
          a.name, a.id, a.version || "", a.available || "",
          a.installed_at || "", a.install_location || "", a.source || "",
          a.local ? a.local.path : "",
          (bk && bk.exists) ? bk.path : "",
          a.local ? (a.local.args || "") : "",
        ]);
      }
      content = "\ufeff" + rows.map(x => x.map(csvCell).join(",")).join("\r\n");
    } else {
      const lines = [];
      lines.push("==================================================");
      lines.push("  软件清单（由 软件备份管理器 导出）");
      lines.push("  导出时间：" + stamp);
      lines.push("  共 " + sel.length + " 个软件");
      lines.push("==================================================");
      lines.push("");
      sel.forEach((a, i) => {
        const bk = BACKUP_IDX[a.id];
        lines.push(`${i + 1}. ${a.name}`);
        lines.push(`     ID        : ${a.id}`);
        lines.push(`     已装版本  : ${a.version || "?"}${a.available ? "   (可更新到 " + a.available + ")" : ""}`);
        if (a.installed_at) {
          lines.push(`     安装日期  : ${a.installed_at}${a.install_location ? "   (" + a.install_location + ")" : ""}`);
        }
        lines.push(`     来源      : ${a.source || "-"}`);
        if (a.local) {
          lines.push(`     本地安装包: ${a.local.path}`);
          lines.push(`     安装命令  : ${a.local.args || "(使用默认)"}`);
        } else if (bk && bk.exists) {
          lines.push(`     已下载包  : ${bk.path}`);
          lines.push("     安装方式  : 用已下载的安装包（离线，无需 winget 源）");
        } else {
          lines.push("     安装方式  : winget 官网源（自动下载最新安装包）");
        }
        lines.push("");
      });
      content = lines.join("\n");
    }

    const w = await api.save_text_file(r.path, content);
    if (w && w.ok) {
      toast("清单已导出：" + r.path.split(/[\\/]/).pop());
      try { await api.open_folder(r.path.replace(/[\\/][^\\/]*$/, "")); } catch (e) {}
    } else {
      toast("导出失败：" + ((w && w.msg) || "未知错误"));
    }
  }

  // ---------- 弹出小窗（随鼠标定位，玻璃风格，与主界面一致） ----------
  function showPop(html, x, y, wide) {
    const pop = $("#pop");
    pop.innerHTML = `<div class="pop-inner">${html}</div>`;
    pop.classList.toggle("wide", !!wide);
    pop.hidden = false;
    // 定位到鼠标右侧，避免超出屏幕
    const w = pop.offsetWidth, h = pop.offsetHeight;
    let nx = x + 16, ny = y - 10;
    if (nx + w > window.innerWidth - 8) nx = Math.max(8, x - w - 16);
    if (ny + h > window.innerHeight - 8) ny = Math.max(8, window.innerHeight - h - 8);
    if (ny < 8) ny = 8;
    pop.style.left = nx + "px";
    pop.style.top = ny + "px";
    return pop;
  }
  function hidePop() { $("#pop").hidden = true; $("#pop").innerHTML = ""; }

  // ---------- 卡片右键菜单 ----------
  function hideCtx() { $("#ctxMenu").hidden = true; $("#ctxMenu").innerHTML = ""; }

  function showCtx(a, x, y) {
    const m = $("#ctxMenu");
    const items = [];
    items.push({ label: a._sel ? "取消勾选" : "勾选这个软件", fn: () => {
      a._sel = !a._sel;
      updateSelInfo(); scheduleSaveSelection();
      if (a._el) a._el.classList.toggle("sel", a._sel);
    }});
    if (a.local) {
      items.push({ label: "📂 打开安装包位置", fn: () => openLinkLoc(a) });
      items.push({ label: "✎ 设置安装方式…", fn: (ev) => editArgs(a, ev) });
      items.push({ label: "✕ 取消本地安装包链接", danger: true, fn: () => unlinkApp(a) });
    } else {
      items.push({ label: "🔗 链接本地安装包…", fn: () => linkApp(a) });
    }
    items.push({ sep: true });
    items.push({ label: "📋 复制软件 ID", fn: () => copyText(a.id) });

    m.innerHTML = items.map((it, i) => it.sep ? "<hr/>"
      : `<button data-i="${i}"${it.danger ? ' class="danger"' : ""}>${esc(it.label)}</button>`
    ).join("");
    m.hidden = false;
    // 贴边时自动翻转，避免超出屏幕
    const w = m.offsetWidth, h = m.offsetHeight;
    let nx = x, ny = y;
    if (nx + w > window.innerWidth - 8) nx = Math.max(8, x - w);
    if (ny + h > window.innerHeight - 8) ny = Math.max(8, window.innerHeight - h - 8);
    m.style.left = nx + "px";
    m.style.top = ny + "px";
    m.querySelectorAll("button[data-i]").forEach(b => {
      b.onclick = () => {
        const it = items[+b.dataset.i];
        hideCtx();
        if (it && it.fn) it.fn({ clientX: x, clientY: y, preventDefault(){} });
      };
    });
  }

  async function copyText(t) {
    try {
      await navigator.clipboard.writeText(t);
      toast("已复制：" + t);
    } catch (e) {
      toast("复制失败");
    }
  }

  // ---------- 点击空白处关闭二级窗口 ----------
  function closeOverlays() {
    if (!$("#pop").hidden) hidePop();
    if (!$("#ctxMenu").hidden) hideCtx();
    $("#appearance").hidden = true;
    // 日志窗口：任务运行中不自动关（正在盯着进度）
    if (!pollTimer) $("#logpanel").hidden = true;
  }

  async function editArgs(a, ev) {
    if (!a.local) return;
    const isZip = /\.(zip|7z|tar|gz|tgz|rar)$/i.test(a.local.path);
    const sampleLoc = ($("#installDir").value.trim() || "D:\\Software") + "\\" + dirKeyJs(a.name);
    if (isZip) {
      // 压缩包无需安装参数：直接解压
      const html = `
        <div class="pop-head"><span>🗜️ ${esc(a.name)}</span><button class="pop-close" data-x>✕</button></div>
        <div class="pop-note">这是 <b>压缩包</b>，重装/安装时会<b>自动解压</b>到该软件自己的文件夹，不需要设置安装参数。点「完成」即可。</div>
        <div class="pop-actions"><button class="btn primary" data-ok>完成</button></div>`;
      const pop = showPop(html, ev.clientX, ev.clientY);
      pop.querySelector("[data-x]").onclick = hidePop;
      pop.querySelector("[data-ok]").onclick = hidePop;
      return;
    }
    const cur = a.local.args || guessArgs(a.local.path);
    const preview = (cur || "")
      .replace("{path}", a.local.path)
      .replace("{loc}", sampleLoc);
    const html = `
      <div class="pop-head"><span>⚙ 安装方式 · ${esc(a.name)}</span><button class="pop-close" data-x>✕</button></div>
      <div class="pop-note">
        一般情况<b>不用改</b>，点「保持默认」即可。<br>
        • <b>{path}</b> = 你的安装包文件<br>
        • <b>{loc}</b> = 装到哪个文件夹（软件会自动放进自己的子文件夹）
      </div>
      <div class="pop-row"><span>常用方案（点一下即可，看不懂就选第一个）</span>
        <div class="pop-preset">
          <button data-preset="">保持默认（推荐新手）</button>
          <button data-preset='"{path}" /S /D="{loc}"'>exe 普通静默安装</button>
          <button data-preset='msiexec /i "{path}" /qn /norestart INSTALLDIR="{loc}"'>msi 静默安装</button>
        </div>
      </div>
      <div class="pop-row"><span>安装命令（可手动改）</span>
        <textarea id="popArgs" rows="3" spellcheck="false">${esc(cur)}</textarea>
      </div>
      <div class="pop-row"><span>实际会执行的样子（预览）</span>
        <div class="pop-preview" id="popPreview"></div>
      </div>
      <div class="pop-actions">
        <button class="btn" data-x>取消</button>
        <button class="btn primary" data-ok>保存</button>
      </div>`;
    const pop = showPop(html, ev.clientX, ev.clientY);
    const ta = pop.querySelector("#popArgs");
    const pv = pop.querySelector("#popPreview");
    const upd = () => { pv.textContent = (ta.value || "")
      .replace("{path}", a.local.path).replace("{loc}", sampleLoc) || "（空=用默认）"; };
    upd();
    pop.querySelectorAll("[data-preset]").forEach(b => {
      b.onclick = () => { ta.value = b.dataset.preset; upd();
        pop.querySelectorAll("[data-preset]").forEach(x => x.classList.remove("active"));
        b.classList.add("active"); };
    });
    pop.querySelector("[data-x]").onclick = hidePop;
    pop.querySelector("[data-ok]").onclick = async () => {
      a.local.args = ta.value.trim();
      await saveLinksQuiet();
      hidePop(); render();
      toast("已保存安装方式");
    };
  }

  // ---------- 总设置（统一设置所有勾选软件） ----------
  async function globalSettings(ev) {
    const curDir = $("#installDir").value.trim();
    const curBak = $("#backupDir").value.trim();
    const html = `
      <div class="pop-head"><span>⚙ 勾选软件 · 统一设置</span><button class="pop-close" data-x>✕</button></div>
      <div class="pop-note">
        勾选的软件都会放进下面这个<b>大文件夹</b>，每个软件自动生成一个<b>独立的小文件夹</b>：
        <b>exe 自动安装</b>、<b>zip 等压缩包自动解压</b>。不管每个软件原本装在哪，这里统一管。
        这里改完会<b>同步回上方工具栏</b>，并自动记住。
      </div>
      <div class="pop-row"><span>📥 安装包保存目录（下载的官网安装包放这里）</span>
        <span class="path-row">
          <input type="text" id="gsBak" spellcheck="false" value="${esc(curBak)}" />
          <button class="browse" id="gsBakBrowse">浏览</button>
        </span>
      </div>
      <div class="pop-row"><span>📂 重装安装根目录（每个软件单独子文件夹）</span>
        <span class="path-row">
          <input type="text" id="gsDir" spellcheck="false" value="${esc(curDir)}" />
          <button class="browse" id="gsBrowse">浏览</button>
        </span>
      </div>
      <label class="pop-check"><input type="checkbox" id="gsFlat" ${settings.flat ? "" : "checked"} />
        每个软件建独立子文件夹（推荐，互不干扰）</label>
      <div class="pop-row"><span>给所有 exe 类统一加静默参数（可选，留空用默认）</span>
        <input type="text" id="gsArgs" spellcheck="false" placeholder='例如 "{path}" /S /D="{loc}"' />
      </div>
      <div class="pop-row"><span>🧩 安装器格式（同一软件有多种安装器时优先下哪种）</span>
        <select id="gsInstType" class="sel-type">
          <option value="auto">自动 · 优先 EXE（推荐）</option>
          <option value="msi">优先 MSI 安装包</option>
          <option value="zip">优先 ZIP 免安装包</option>
          <option value="all">全部变体（不筛选）</option>
        </select>
        <div class="pop-hint">同一个软件常常同时提供 EXE / MSI / ZIP 几种安装器。
          选了某种格式后，如果该软件没有这种安装器，会<b>自动换下一种</b>，不会下载失败。</div>
      </div>
      <div class="pop-actions"><button class="btn primary" data-ok>完成</button></div>`;
    const pop = showPop(html, ev.clientX, ev.clientY);
    const gsType = pop.querySelector("#gsInstType");
    if (gsType) gsType.value = settings.installerType || "auto";
    pop.querySelector("[data-x]").onclick = hidePop;
    pop.querySelector("#gsBrowse").onclick = async () => {
      const p = await api.pick_folder("选择大文件夹");
      if (p) pop.querySelector("#gsDir").value = p;
    };
    pop.querySelector("#gsBakBrowse").onclick = async () => {
      const p = await api.pick_folder("选择安装包保存目录");
      if (p) pop.querySelector("#gsBak").value = p;
    };
    pop.querySelector("[data-ok]").onclick = async () => {
      const dir = pop.querySelector("#gsDir").value.trim();
      const bak = pop.querySelector("#gsBak").value.trim();
      if (dir) { $("#installDir").value = dir; settings.installDir = dir; }
      if (bak) { $("#backupDir").value = bak; settings.backupDir = bak; }
      const flat = !pop.querySelector("#gsFlat").checked;
      settings.flat = flat;
      if (api.apply_global) await api.apply_global({ flat });
      // 给所有「已勾选 + 链接的 exe 类」统一套用静默参数
      const gargs = pop.querySelector("#gsArgs").value.trim();
      if (gargs) {
        let n = 0;
        for (const a of APPS) {
          if (a._sel && a.local && !/\.(zip|7z|tar|gz|tgz|rar)$/i.test(a.local.path)) {
            a.local.args = gargs; n++;
          }
        }
        if (n) await saveLinksQuiet();
      }
      // 安装器格式：总设置与工具栏保持同步
      const itSel = pop.querySelector("#gsInstType");
      if (itSel) {
        settings.installerType = itSel.value || "auto";
        const bar = $("#instType");
        if (bar) bar.value = settings.installerType;
      }
      persistSettings();
      hidePop(); render();
      toast(flat ? "已设置：每个软件独立子文件夹" : "已设置：全部装进同一文件夹");
    };
  }

  // ---------- 渲染列表 ----------
  // 版本号排序：按数字段逐个比，"1.10" 要排在 "1.9" 后面，不能按字符串比
  function verNum(v) {
    return String(v || "").split(/[.\-+]/).map(x => parseInt(x, 10) || 0);
  }
  function cmpVer(a, b) {
    const x = verNum(a), y = verNum(b);
    const n = Math.max(x.length, y.length);
    for (let i = 0; i < n; i++) {
      const d = (x[i] || 0) - (y[i] || 0);
      if (d) return d;
    }
    return 0;
  }

  // 安装时间范围筛选：返回该应用是否落在范围内
  function inDateRange(a) {
    if (dateRange === "all") return true;
    if (dateRange === "unknown") return !a.installed_ts;
    if (dateRange === "has") return !!a.installed_ts;
    if (!a.installed_ts) return false;      // 有范围时，没日期的排出去
    if (dateRange === "older") {
      return a.installed_ts < Date.now() / 1000 - 365 * 86400;
    }
    const days = parseInt(dateRange, 10);
    if (!days) return true;
    return a.installed_ts >= Date.now() / 1000 - days * 86400;
  }

  function sortApps(items) {
    const byName = (x, y) => x.name.localeCompare(y.name, "zh-CN");
    const arr = items.slice();
    switch (sortBy) {
      case "name_desc":
        arr.sort((x, y) => byName(y, x));
        break;
      case "date_desc":
        // 没安装日期的沉到最后
        arr.sort((x, y) => {
          const xt = x.installed_ts || 0, yt = y.installed_ts || 0;
          if (!xt && !yt) return byName(x, y);
          if (!xt) return 1;
          if (!yt) return -1;
          return yt - xt || byName(x, y);
        });
        break;
      case "date_asc":
        arr.sort((x, y) => {
          const xt = x.installed_ts || 0, yt = y.installed_ts || 0;
          if (!xt && !yt) return byName(x, y);
          if (!xt) return 1;
          if (!yt) return -1;
          return xt - yt || byName(x, y);
        });
        break;
      case "version_desc":
        arr.sort((x, y) => cmpVer(y.version, x.version) || byName(x, y));
        break;
      case "size_desc":
        arr.sort((x, y) => (backupSize(y) - backupSize(x)) || byName(x, y));
        break;
      default:
        arr.sort(byName);
    }
    return arr;
  }

  function backupSize(a) {
    const b = BACKUP_IDX[a.id];
    return (b && b.size) || 0;
  }

  function render() {
    const list = $("#list");
    const q = query.trim().toLowerCase();
    let items = APPS.filter(a => {
      if (filter === "winget" && !a.winget) return false;
      if (filter === "upd" && !(a.available && !a.local)) return false;
      if (onlyBackup && !(BACKUP_IDX[a.id] && BACKUP_IDX[a.id].exists)) return false;
      if (!inDateRange(a)) return false;
      if (q && !(a.name.toLowerCase().includes(q) || a.id.toLowerCase().includes(q))) return false;
      return true;
    });
    items = sortApps(items);

    if (!APPS.length) return;
    list.innerHTML = "";
    if (!items.length) {
      let hint = "没有匹配的应用";
      if (onlyBackup) {
        hint = BACKUP_IDX && Object.keys(BACKUP_IDX).length
          ? "没有已备份的应用（已备份的软件不在「已装应用」列表里）"
          : "还没有下载过任何安装包，先勾选软件点「⬇ 下载所选安装包」";
      } else if (dateRange !== "all") {
        hint = "该安装时间范围内没有应用";
      }
      list.innerHTML = `<div class="loading"><p>${esc(hint)}</p></div>`;
      return;
    }

    for (const a of items) {
      const card = document.createElement("div");
      const selectable = a.winget || a.local;
      card.className = "card" + (a._sel ? " sel" : "") + (selectable ? "" : " disabled") +
                       (a._linkDead ? " broken" : "");
      card.dataset.id = a.id;
      a._el = card;

      const badge = a.local
        ? '<span class="badge link">已链接</span>'
        : a.winget
          ? '<span class="badge auto">可自动</span>'
          : '<span class="badge manual">需手动</span>';
      const upd = (!a.local && a.available) ? '<span class="badge upd">可更新</span>' : "";
      const dead = a._linkDead
        ? '<span class="badge broken" title="链接的安装包已不存在，可能被移动或删除。点 🔗 可重新链接">链接失效</span>'
        : "";
      // 已经下载过安装包 —— 一键安装/导出脚本会直接用这个本地包（离线）
      const bk = BACKUP_IDX[a.id];
      const bkBadge = (bk && bk.exists)
        ? `<span class="badge backup" title="已下载的安装包：${esc(bk.path)}\\n安装与重装脚本会优先用它（离线安装）">已备份</span>`
        : "";

      const isArch = a.local && /\.(zip|7z|tar|gz|tgz|rar)$/i.test(a.local.path);
      const archMark = isArch
        ? ' <span class="arch-mark" title="这是压缩包：安装/重装时会自动解压到该软件自己的文件夹，无需手动">🗜️</span>'
        : "";
      const linkLine = a.local
        ? `<div class="linkline">🔗 已链接：<span class="lfname${a._linkDead ? " dead" : ""}">${esc(a.local.path.split(/[\\/]/).pop())}</span>${archMark}${dead}
             <span class="link-acts">
               <button class="lkopen" title="打开保存位置"${a._linkDead ? " disabled" : ""}>📂</button>
               <button class="lkedit" title="设置安装方式">✎</button>
               <button class="lkdel" title="取消链接">✕</button>
             </span></div>`
        : "";

      // 第二行：ID + 版本 + 安装日期
      // ID 有时很长，用 flex 让它自己截断，日期始终留在可见处
      const dateTip = a.installed_at
        ? ` <span class="date" title="安装于 ${esc(a.installed_at)}${a.install_location ? " · " + esc(a.install_location) : ""}">· ${esc(a.installed_at)}</span>`
        : "";

      const st = a._status ? `<span class="status ${a._statusCls}">${a._status}</span>` : "";

      card.innerHTML = `
        <div class="check"></div>
        <div class="info">
          <div class="name"><span class="ntext">${esc(a.name)}</span>${badge}${upd}${bkBadge}</div>
          <div class="meta"><span class="mid">${esc(a.id)} · 已装 ${esc(a.version || "?")}</span>${dateTip}</div>
          ${linkLine}
        </div>
        <button class="lkbtn" title="链接本地安装包（固定此版本）">🔗</button>
        ${st}`;

      // 卡片点击：切换勾选（链接或 winget 均可选）
      if (selectable) {
        card.addEventListener("click", (e) => {
          if (e.target.closest(".lkbtn, .lkedit, .lkdel, .lkopen")) return;
          a._sel = !a._sel;
          card.classList.toggle("sel", a._sel);
          updateSelInfo();
          scheduleSaveSelection();
        });
      }
      // 链接按钮
      card.querySelector(".lkbtn").addEventListener("click", (e) => {
        e.stopPropagation();
        linkApp(a);
      });
      // 右键菜单
      card.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        e.stopPropagation();
        showCtx(a, e.clientX, e.clientY);
      });
      const op = card.querySelector(".lkopen");
      if (op) op.addEventListener("click", (e) => { e.stopPropagation(); openLinkLoc(a); });
      const ed = card.querySelector(".lkedit");
      if (ed) ed.addEventListener("click", (e) => { e.stopPropagation(); editArgs(a, e); });
      const dl = card.querySelector(".lkdel");
      if (dl) dl.addEventListener("click", (e) => { e.stopPropagation(); unlinkApp(a); });

      list.appendChild(card);
    }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // 卡片级状态：只改状态徽标，不整页重绘（避免闪动）
  function paintStatus(a) {
    const el = a._el;
    if (!el || !el.isConnected) return;
    let span = el.querySelector(".status");
    if (!a._status) { if (span) span.remove(); return; }
    if (!span) {
      span = document.createElement("span");
      span.className = "status";
      el.appendChild(span);
    }
    span.className = "status " + (a._statusCls || "wait");
    span.textContent = a._status;
  }

  function statusFromResult(r) {
    if (!r) return null;
    if (r.status === "完成") return ["✔ 完成", "done"];
    if (String(r.status).startsWith("完成")) return ["⚠ 完成", "done"];
    if (r.status === "失败") return ["✘ 失败", "fail"];
    return [r.status, "wait"];
  }

  function refreshStatuses(results) {
    for (const a of APPS) {
      const s = statusFromResult(results[a.id]);
      if (s) { a._status = s[0]; a._statusCls = s[1]; }
      else { a._status = ""; a._statusCls = ""; }
    }
    render();
  }

  // 运行中：当前项显示「进行中」，已完成的显示结果
  function applyLiveStatus(p) {
    const res = p.results || {};
    for (const a of APPS) {
      const s = statusFromResult(res[a.id]);
      if (s) { a._status = s[0]; a._statusCls = s[1]; }
      else if (p.current_id && a.id === p.current_id) { a._status = "⏳ 进行中"; a._statusCls = "run"; }
      else { a._status = ""; a._statusCls = ""; }
      paintStatus(a);
    }
  }

  // ---------- 进度轮询 ----------
  function startPolling() {
    if (pollTimer) return;
    $("#progressWrap").hidden = false;
    pollTimer = setInterval(async () => {
      try {
        const p = await api.get_progress();
        renderProgress(p);
        if (p.running) {
          applyLiveStatus(p);
        } else {
          clearInterval(pollTimer); pollTimer = null;
          setButtons(true);
          refreshStatuses(p.results || {});
          $("#progressText").textContent = "任务结束";
          // 下载任务结束后刷新「已下载安装包」索引，让徽标实时出现
          if (p.phase === "download") refreshBackupIndex();
        }
      } catch (e) { /* ignore */ }
    }, 400);
  }

  async function refreshBackupIndex() {
    try {
      const bi = await api.get_backup_index();
      if (bi && typeof bi === "object") {
        BACKUP_IDX = bi;
        render();
      }
    } catch (e) { /* 忽略 */ }
  }

  function renderProgress(p) {
    const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
    $("#progressBar").style.width = pct + "%";
    $("#progressText").textContent = p.current || (p.phase === "download" ? "下载中…" : "安装中…");
    $("#progressCount").textContent = p.total ? `${p.done}/${p.total} (${pct}%)` : "";
    const log = $("#log");
    log.textContent = (p.log || []).join("\n");
    log.scrollTop = log.scrollHeight;
  }

  function setButtons(enabled) {
    ["#btnDownload", "#btnExport", "#btnInstall", "#btnSelAll", "#btnInvert",
     "#btnBatchLink", "#btnExportList"].forEach(s => {
      $(s).disabled = !enabled;
    });
  }
  // ---------- 最大化状态同步（图标切换 + 最大化时禁用缩放手柄） ----------
  async function syncMaxState() {
    const btn = $("#btnMax");
    const rz = $("#rzHandles");
    if (!btn || !rz) return;
    let maxed = false;
    try {
      if (api && api.is_maximized) {
        const r = await api.is_maximized();
        maxed = !!(r && r.maximized);
      }
    } catch (e) { /* 忽略 */ }
    btn.classList.toggle("maximized", maxed);
    btn.title = maxed ? "还原" : "最大化";
    rz.classList.toggle("max", maxed);
  }

  // ---------- 事件绑定 ----------
  function bind() {
    // 标题栏拖动：调用后端 begin_drag，由后端线程轮询光标移动窗口
    // （pywebview 6.x 已移除 start_drag，自带的 easy_drag 又会跟界面元素冲突）
    $("#titlebar").addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      if (e.target.closest(".win-btns, .rz")) return;
      e.preventDefault();
      window.__dragging = true;
      if (api && api.begin_drag) api.begin_drag("move", e.clientX, e.clientY);
    });
    // 拖动过程中把光标变成移动样式，松开后恢复
    document.addEventListener("mousemove", () => {
      if (window.__dragging) document.body.style.cursor = "move";
    });
    window.addEventListener("mouseup", () => {
      window.__dragging = false;
      document.body.style.cursor = "";
      if (api && api.end_drag) api.end_drag();
    });

    // 八个方向的缩放手柄
    document.querySelectorAll("#rzHandles i").forEach(h => {
      h.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        window.__dragging = true;
        if (api && api.begin_drag) api.begin_drag(h.dataset.dir, e.clientX, e.clientY);
      });
    });

    $("#btnMin").addEventListener("click", () => api && api.minimize());
    $("#btnMax").addEventListener("click", async () => {
      if (!api || !api.toggle_maximize) return;
      await api.toggle_maximize();
      await syncMaxState();
    });
    $("#btnClose").addEventListener("click", () => api && api.close_window());

    // 外观面板
    $("#btnGear").addEventListener("click", () => {
      $("#appearance").hidden = !$("#appearance").hidden;
      if (!$("#appearance").hidden) syncSystemTheme();
    });
    $("#btnApClose").addEventListener("click", () => { $("#appearance").hidden = true; });

    // 点空白处关闭二级窗口（外观面板 / 弹出小窗 / 日志 / 右键菜单）
    // 排除：窗口右上角按钮、弹出小窗自身、外观面板自身，以及各自的开关键（避免误关）
    document.addEventListener("mousedown", (e) => {
      const t = e.target;
      if (t && t.closest &&
          t.closest("#pop, #appearance, #logpanel, #ctxMenu, .win-btns, #btnGlobal, #btnBatchLink, #btnLog")) return;
      closeOverlays();
    });

    // 外观模式：浅色 / 深色 / 跟随系统
    $("#modeSeg").addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-mode]");
      if (!b) return;
      const mode = b.dataset.mode;
      if (mode === "auto") await syncSystemTheme();
      applyMode(mode);
      if (mode === "auto")
        toast(`已跟随系统（当前系统：${sysTheme === "dark" ? "深色" : "浅色"}）`);
      else if (mode === "light") toast("已切换为浅色玻璃");
      else toast("已切换为深色玻璃");
    });

    $("#btnPickImg").addEventListener("click", async () => {
      const r = await api.pick_image();
      if (r && r.dataUri) {
        settings.bgImagePath = r.path || "";
        applyBgImage(r.dataUri, settings.bgOpacity ?? 100);
        persistSettings();
        toast("已设置背景图片");
      } else if (r && r.error) {
        toast("选择失败：" + r.error);
      }
    });
    $("#btnClearImg").addEventListener("click", () => {
      settings.bgImagePath = "";
      applyBgImage("", settings.bgOpacity ?? 100);
      persistSettings();
      toast("已清除背景图片");
    });
    $("#bgOpacity").addEventListener("input", (e) => {
      const v = +e.target.value;
      document.documentElement.style.setProperty("--bgimg", (v / 100).toFixed(3));
      $("#bgOpacityVal").textContent = v + "%";
      settings.bgOpacity = v;
      persistSettings();
    });

    // 界面通透度（实时）
    $("#panelAlpha").addEventListener("input", (e) => {
      const v = +e.target.value;
      document.documentElement.style.setProperty("--panel-alpha", (v / 100).toFixed(3));
      $("#panelAlphaVal").textContent = v + "%";
      settings.panelAlpha = v;
      persistSettings();
    });

    // 透出桌面（重启生效）
    $("#deskTrans").addEventListener("change", (e) => {
      settings.desktopTransparent = e.target.checked;
      persistSettings();
      if (e.target.checked) {
        toast("已开启：下次双击 run.bat 启动后将透出桌面");
      }
    });

    // 目录输入框：记住并同步
    $("#backupDir").addEventListener("input", () => {
      settings.backupDir = $("#backupDir").value.trim();
      persistSettings();
    });
    $("#installDir").addEventListener("input", () => {
      settings.installDir = $("#installDir").value.trim();
      persistSettings();
    });

    // 安装器格式：同一个软件有多种安装器时，优先下哪一种
    $("#instType").addEventListener("change", (e) => {
      settings.installerType = e.target.value || "auto";
      persistSettings();
      const lab = $("#instType").selectedOptions[0];
      toast("安装器格式：" + (lab ? lab.textContent : settings.installerType));
    });

    $("#search").addEventListener("input", (e) => { query = e.target.value; render(); });
    $("#btnClear").addEventListener("click", () => { query = ""; $("#search").value = ""; render(); });

    $("#chipAll").addEventListener("click", () => {
      filter = "all";
      $("#chipAll").classList.add("active");
      $("#chipWinget").classList.remove("active");
      $("#chipUpd").classList.remove("active");
      render();
    });
    $("#chipWinget").addEventListener("click", () => {
      filter = "winget";
      $("#chipWinget").classList.add("active");
      $("#chipAll").classList.remove("active");
      $("#chipUpd").classList.remove("active");
      render();
    });
    $("#chipUpd").addEventListener("click", () => {
      filter = "upd";
      $("#chipUpd").classList.add("active");
      $("#chipAll").classList.remove("active");
      $("#chipWinget").classList.remove("active");
      render();
    });

    // ---------- 排序 ----------
    $("#sortBy").addEventListener("change", (e) => {
      sortBy = e.target.value || "name";
      settings.sortBy = sortBy;
      persistSettings();
      render();
    });

    // ---------- 安装时间范围筛选 ----------
    $("#dateRange").addEventListener("change", (e) => {
      dateRange = e.target.value || "all";
      settings.dateRange = dateRange;
      persistSettings();
      render();
      const lab = $("#dateRange").selectedOptions[0];
      if (lab && dateRange !== "all") toast("按安装时间筛选：" + lab.textContent);
    });

    // ---------- 仅看已下载过安装包的 ----------
    $("#btnOnlyBackup").addEventListener("click", () => {
      onlyBackup = !onlyBackup;
      $("#btnOnlyBackup").classList.toggle("active", onlyBackup);
      render();
      if (onlyBackup) {
        const n = Object.keys(BACKUP_IDX).filter(k => BACKUP_IDX[k] && BACKUP_IDX[k].exists).length;
        if (!n) toast("还没有下载过任何安装包，先点「下载所选安装包」");
      }
    });

    $("#btnSelAll").addEventListener("click", () => {
      APPS.forEach(a => { if (a.winget || a.local) a._sel = true; });
      render(); updateSelInfo(); scheduleSaveSelection();
    });
    $("#btnInvert").addEventListener("click", () => {
      APPS.forEach(a => { if (a.winget || a.local) a._sel = !a._sel; });
      render(); updateSelInfo(); scheduleSaveSelection();
    });
    $("#btnBatchLink").addEventListener("click", (e) => batchLink(e));

    $("#btnGlobal").addEventListener("click", (e) => {
      if (!APPS.some(a => a._sel && (a.winget || a.local)))
        return toast("请先勾选至少一个软件，再做统一设置");
      globalSettings(e);
    });

    // 以管理员身份运行（自动重启，无需手工）
    $("#btnElevate").addEventListener("click", async () => {
      const r = await api.elevate();
      if (r && r.ok) {
        toast("正在以管理员身份重启…");
        if (api.close_window) api.close_window();
      } else {
        toast("提权失败：" + ((r && r.error) || "未知错误"));
      }
    });

    document.querySelectorAll(".browse").forEach(b => {
      b.addEventListener("click", async () => {
        const target = b.dataset.target;
        const path = await api.pick_folder("选择文件夹");
        if (path) {
          $("#" + target).value = path;
          if (target === "backupDir") settings.backupDir = path;
          else settings.installDir = path;
          persistSettings();
        }
      });
    });

    $("#btnDownload").addEventListener("click", async () => {
      const p = payload();
      if (!p.length) return toast("请先勾选应用（可自动下载，或已链接本地安装包）");
      const backup = $("#backupDir").value.trim();
      if (!backup) return toast("请填写安装包保存目录");
      const r = await api.start_download(p, backup, settings.installerType || "auto");
      if (!r.ok) return toast(r.msg);
      setButtons(false); startPolling();
    });

    $("#btnInstall").addEventListener("click", async () => {
      const p = payload();
      if (!p.length) return toast("请先勾选应用（可自动下载，或已链接本地安装包）");
      const inst = $("#installDir").value.trim();
      if (!inst) return toast("请填写重装安装根目录");
      const r = await api.start_install(p, inst, settings.installerType || "auto");
      if (!r.ok) return toast(r.msg);
      setButtons(false); startPolling();
      // 提示会优先用哪些本地包
      const offline = p.filter(a => !a.local && BACKUP_IDX[a.id] && BACKUP_IDX[a.id].exists).length;
      if (offline) toast(`${offline} 个将用已下载的安装包（离线），其余走 winget 官网源`);
    });

    $("#btnExport").addEventListener("click", async () => {
      const p = payload();
      if (!p.length) return toast("请先勾选应用（可自动下载，或已链接本地安装包）");
      const backup = $("#backupDir").value.trim();
      const inst = $("#installDir").value.trim();
      if (!backup || !inst) return toast("请填写两个目录");
      const r = await api.export_script(p, backup, inst, settings.installerType || "auto");
      if (r.ok) {
        toast("重装脚本已生成");
        api.open_folder(backup);
      } else toast("导出失败：" + (r.msg || ""));
    });

    $("#btnExportList").addEventListener("click", () => exportList());

    $("#btnLog").addEventListener("click", () => { $("#logpanel").hidden = !$("#logpanel").hidden; });
    $("#btnLogClose").addEventListener("click", () => { $("#logpanel").hidden = true; });

    // ---------- 键盘快捷键 ----------
    document.addEventListener("keydown", (e) => {
      const inField = /^(INPUT|TEXTAREA|SELECT)$/.test(
        (e.target && e.target.tagName) || "");
      // Esc：关闭所有二级窗口
      if (e.key === "Escape") {
        hidePop();
        hideCtx();
        $("#appearance").hidden = true;
        if (!pollTimer) $("#logpanel").hidden = true;
        if (inField) e.target.blur();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
        e.preventDefault();
        $("#search").focus();
        $("#search").select();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a" && !inField) {
        e.preventDefault();
        APPS.forEach(a => { if (a.winget || a.local) a._sel = true; });
        render(); updateSelInfo(); scheduleSaveSelection();
      }
    });
  }

  // ---------- 启动 ----------
  async function boot() {
    buildSwatches();
    bind();
    makeDraggable($("#appearance"), $("#appearance .ap-head"));

    // 载入已保存的外观设置
    try {
      const saved = await api.get_settings();
      if (saved && typeof saved === "object") {
        settings.theme = saved.theme || "galaxy";
        settings.themeMode = saved.themeMode || "auto";
        settings.bgImagePath = saved.bgImagePath || "";
        settings.bgOpacity = (saved.bgOpacity == null ? 100 : saved.bgOpacity);
        settings.panelAlpha = (saved.panelAlpha == null ? 100 : saved.panelAlpha);
        settings.desktopTransparent = !!saved.desktopTransparent;
        settings.flat = !!saved.flat;
        settings.backupDir = saved.backupDir || "";
        settings.installDir = saved.installDir || "";
        settings.sortBy = saved.sortBy || "name";
        settings.dateRange = saved.dateRange || "all";
        sortBy = settings.sortBy;
        dateRange = settings.dateRange;
      }
      // 把 flat 设置同步给后端（影响安装/导出时是否建独立子文件夹）
      if (api && api.apply_global) {
        try { await api.apply_global({ flat: settings.flat }); } catch (e) {}
      }
    } catch (e) { /* 忽略 */ }

    // 先探测 Windows 系统主题，再套用外观模式（保证「跟随系统」正确）
    await syncSystemTheme();
    applyMode(settings.themeMode);
    startSysThemeWatch();

    // 界面通透度
    document.documentElement.style.setProperty("--panel-alpha", (settings.panelAlpha / 100).toFixed(3));
    $("#panelAlpha").value = settings.panelAlpha;
    $("#panelAlphaVal").textContent = settings.panelAlpha + "%";
    // 透出桌面模式（html + body 都要透明，否则 html 的深色底会透出来）
    if (settings.desktopTransparent) {
      document.documentElement.classList.add("transparent-mode");
      document.body.classList.add("transparent-mode");
      $("#deskTrans").checked = true;
    }

    applyTheme(settings.theme);
    if (settings.bgImagePath) {
      try {
        const uri = await api.encode_image(settings.bgImagePath);
        applyBgImage(uri, settings.bgOpacity);
      } catch (e) {
        applyBgImage("", settings.bgOpacity);
      }
    } else {
      applyBgImage("", settings.bgOpacity);
    }

    const def = await api.get_defaults();
    $("#backupDir").value = settings.backupDir || def.backup;
    $("#installDir").value = settings.installDir || def.install;
    settings.backupDir = $("#backupDir").value;
    settings.installDir = $("#installDir").value;
    // 安装器格式（auto / msi / zip / all）
    const itSel = $("#instType");
    if (itSel) {
      itSel.value = settings.installerType || def.installerType || "auto";
      settings.installerType = itSel.value;
    }
    // 排序 / 安装时间范围（记忆上次的选择）
    const sbSel = $("#sortBy");
    if (sbSel) {
      sbSel.value = settings.sortBy || "name";
      sortBy = sbSel.value;
    }
    const drSel = $("#dateRange");
    if (drSel) {
      drSel.value = settings.dateRange || "all";
      dateRange = drSel.value;
    }
    if (def.admin === false) $("#warnbar").hidden = false;

    let apps = [];
    try {
      apps = await api.get_apps();
    } catch (e) {
      $("#list").innerHTML = '<div class="loading"><p>读取已安装应用失败，请确认 winget 可用后重试。</p></div>';
      console.error(e);
      return;
    }
    APPS = (apps || []).map(a => ({
      name: a.name, id: a.id, version: a.version, available: a.available,
      source: a.source, winget: !!a.winget, _sel: false, _status: "", _statusCls: "",
      local: null, _linkDead: false, _el: null,
      // 后端从注册表读到的安装元数据
      installed_at: a.installed_at || "",
      installed_ts: a.installed_ts || 0,
      install_location: a.install_location || "",
      publisher: a.publisher || "",
    }));

    // 载入已保存的本地安装包链接（按 winget id 关联）
    let restoredLinks = 0;
    try {
      const links = await api.get_links();
      if (links && typeof links === "object") {
        for (const a of APPS) {
          if (links[a.id]) { a.local = links[a.id]; restoredLinks++; }
        }
      }
    } catch (e) { /* 忽略 */ }

    // 恢复上次的勾选（勾选记忆）
    let restoredSel = 0;
    try {
      const selIds = await api.load_selection();
      if (selIds && selIds.length) {
        const set = new Set(selIds);
        for (const a of APPS) {
          if (set.has(a.id) && (a.winget || a.local)) { a._sel = true; restoredSel++; }
        }
      }
    } catch (e) { /* 忽略 */ }

    // 检测失效链接
    const dead = await checkLinkValidity();

    // 读取「已下载安装包」索引（用于显示已备份徽标 + 仅已备份筛选）
    try {
      const bi = await api.get_backup_index();
      if (bi && typeof bi === "object") BACKUP_IDX = bi;
    } catch (e) { /* 忽略 */ }

    const ld = $("#loading"); if (ld) ld.remove();
    render();
    updateSelInfo();
    toast(`已读取 ${APPS.length} 个应用，其中 ${APPS.filter(a => a.winget).length} 个可自动下载`);
    if (restoredLinks) toast(`已恢复 ${restoredLinks} 个本地安装包链接`);
    if (restoredSel) toast(`已恢复上次勾选的 ${restoredSel} 个软件`);
    if (dead) toast(`⚠ 有 ${dead} 个链接的安装包已不存在，请重新链接`);
    const bkCount = Object.keys(BACKUP_IDX).filter(k => BACKUP_IDX[k] && BACKUP_IDX[k].exists).length;
    if (bkCount) toast(`📥 ${bkCount} 个软件已有下载好的安装包，一键安装会直接用（离线）`);
  }

  function start() {
    if (api) return;
    if (!(window.pywebview && window.pywebview.api)) return; // 尚未就绪，等 pywebviewready
    api = window.pywebview.api;
    boot();
  }

  // pywebview 派发的就绪事件是小写 "pywebviewready"（大小写敏感）
  if (window.pywebview && window.pywebview.api) {
    start();
  } else {
    window.addEventListener("pywebviewready", start);
    window.addEventListener("load", start);
  }
})();
