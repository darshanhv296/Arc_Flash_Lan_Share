(function () {
  "use strict";

  let currentTab = "send";
  let lastState = null;
  let folderZipMode = true;
  let lastRenderedQrPayload = "";
  let toastTimer = null;
  let selectedSaveDir = "";
  let stateRefreshInFlight = false;
  let stateRefreshPending = false;
  let stateRefreshTimer = null;

  const $ = (id) => document.getElementById(id);

  function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let val = bytes;
    let idx = 0;
    while (val >= 1024 && idx < units.length - 1) {
      val /= 1024;
      idx++;
    }
    return idx === 0 ? `${Math.round(val)} B` : `${val.toFixed(1)} ${units[idx]}`;
  }

  function formatSpeed(bps) {
    if (!bps || bps <= 0) return "0 B/s";
    return `${formatBytes(bps)}/s`;
  }

  function formatDuration(ms) {
    const totalSec = Math.max(0, Math.floor((ms || 0) / 1000));
    const min = Math.floor(totalSec / 60);
    const sec = totalSec % 60;
    return `${String(min).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  }

  function showToast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
  }

  async function apiJson(url, method = "GET", bodyObj = null) {
    const opts = { method, headers: {} };
    if (bodyObj !== null) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(bodyObj);
    }
    const res = await fetch(url, opts);
    return res.json();
  }

  async function refreshState() {
    if (stateRefreshTimer !== null) {
      clearTimeout(stateRefreshTimer);
      stateRefreshTimer = null;
    }
    if (stateRefreshInFlight) {
      stateRefreshPending = true;
      return;
    }

    stateRefreshInFlight = true;
    $("live-status").textContent = "Updating…";
    $("live-status").classList.remove("error");
    try {
      const response = await fetch("/api/state", { cache: "no-store" });
      if (!response.ok) throw new Error(`State request failed (${response.status})`);
      const state = await response.json();
      lastState = state;
      renderState(state);
      $("live-status").textContent = "Live";
    } catch (error) {
      $("live-status").textContent = "Reconnecting…";
      $("live-status").classList.add("error");
      console.warn("Live state refresh failed:", error);
    } finally {
      stateRefreshInFlight = false;
      if (stateRefreshPending) {
        stateRefreshPending = false;
        refreshState();
      } else {
        stateRefreshTimer = setTimeout(refreshState, 500);
      }
    }
  }

  function renderState(state) {
    document.documentElement.setAttribute("data-theme", state.theme || "system");

    // Header pairing pill
    const pill = $("pairing-pill");
    const pillText = $("pairing-pill-text");
    pill.classList.toggle("paired", !!state.paired);
    pill.classList.toggle("unpaired", !state.paired);
    pillText.textContent = state.paired ? (state.pairedPeerName || "Paired") : "Not paired";

    // Receive tab badge count
    const rxCount = (state.receiveItems || []).length;
    $("tab-btn-receive").textContent = rxCount > 0 ? `Receive (${rxCount})` : "Receive";

    // Connection strip (Zero IP/Port exposed!)
    const strip = $("connection-strip");
    strip.classList.toggle("paired", !!state.paired);
    $("conn-paired-view").classList.toggle("hidden", !state.paired);
    $("conn-unpaired-view").classList.toggle("hidden", !!state.paired);

    if (state.paired) {
      $("connected-peer-name").textContent = state.pairedPeerName || "Paired Device";
      $("connected-via-label").textContent = `Connected · ${state.lanStatus?.transportLabel || "Wi-Fi / LAN"}`;
    } else {
      const peers = state.availablePeers || [];
      const lanOk = state.lanStatus?.isLanConnected;
      if (!lanOk) {
        $("lan-status-text").textContent = "No Wi-Fi / LAN network detected";
      } else if (peers.length > 0) {
        $("lan-status-text").textContent = `Available LAN devices (${peers.length}) — click Connect`;
      } else {
        $("lan-status-text").textContent = `Scanning ${state.lanStatus?.transportLabel || "LAN"} for nearby devices…`;
      }

      const chipsHost = $("peers-chips");
      chipsHost.replaceChildren();
      for (const peer of peers) {
        const btn = document.createElement("button");
        btn.className = "peer-chip";
        btn.textContent = `${peer.displayName} · Connect`;
        btn.onclick = async () => {
          const res = await apiJson("/api/connect", "POST", { deviceId: peer.deviceId });
          showToast(res.message || (res.ok ? "Connected" : "Connect failed"));
          refreshState();
        };
        chipsHost.appendChild(btn);
      }
    }

    // Category buttons enabled only when paired
    const canSend = !!state.paired;
    document.querySelectorAll(".cat-tile").forEach((b) => {
      b.disabled = !canSend;
    });
    $("pair-required-hint").classList.toggle("hidden", canSend);

    // Show/hide send categories for active tab
    $("send-categories-section").classList.toggle("hidden", currentTab !== "send");

    // Active tab metrics & items
    const items = currentTab === "send" ? (state.sendItems || []) : (state.receiveItems || []);
    const summary = currentTab === "send" ? state.sendSummary : state.receiveSummary;

    // Toolbar controls
    $("btn-pause-resume").disabled = !state.canControlTransfers;
    $("btn-stop-all").disabled = !state.canControlTransfers;
    $("btn-clear").disabled = items.length === 0;
    $("pause-icon").classList.toggle("hidden", !!state.batchPaused);
    $("resume-icon").classList.toggle("hidden", !state.batchPaused);
    $("btn-pause-resume").title = state.batchPaused ? "Resume transfers" : "Pause transfers";

    // Dashboard metrics
    $("dash-files").textContent = `${summary.doneCount} / ${summary.totalCount}`;
    if (summary.totalBytes > 0 && summary.transferredBytes > 0 && summary.transferredBytes < summary.totalBytes) {
      $("dash-size").textContent = `${formatBytes(summary.transferredBytes)} / ${formatBytes(summary.totalBytes)}`;
    } else {
      $("dash-size").textContent = formatBytes(summary.totalBytes);
    }
    $("dash-speed").textContent = formatSpeed(summary.speedBytesPerSec);
    $("dash-time").textContent = formatDuration(summary.elapsedMs);
    $("stats-text").textContent = `Total: ${summary.totalCount} | Active: ${summary.activeCount} | Done: ${summary.doneCount} | Failed: ${summary.failedCount}`;
    $("tuning-badge").textContent = state.tuningSummary || "AF01 · 4 MB buf · 32 MB win";
    $("overall-progress-fill").style.width = `${Math.round((summary.overallProgress || 0) * 100)}%`;

    // File list or empty state
    const listEl = $("files-list");
    const emptyEl = $("empty-state");
    if (items.length === 0) {
      listEl.classList.add("hidden");
      emptyEl.classList.remove("hidden");
      if (currentTab === "send") {
        $("empty-icon").textContent = "⚡";
        $("empty-title").textContent = "Ready for high-speed LAN transfer";
        $("empty-subtitle").textContent = canSend
          ? "Connected! Choose Files or Folders above to start sharing."
          : "Connect to an available LAN device above or show your QR code to start sending.";
      } else {
        $("empty-icon").textContent = "📡";
        $("empty-title").textContent = "Waiting for incoming transfers…";
        $("empty-subtitle").textContent = `Files sent from a connected peer save automatically to ${state.saveDir}.`;
      }
    } else {
      emptyEl.classList.add("hidden");
      listEl.classList.remove("hidden");
      listEl.replaceChildren();
      for (const item of items) {
        listEl.appendChild(renderFileRow(item));
      }
    }

    // Update QR modal if open
    if (!$("modal-qr").classList.contains("hidden")) {
      renderQrModal(state);
    }
  }

  function renderFileRow(item) {
    const row = document.createElement("div");
    row.className = "file-row";

    const top = document.createElement("div");
    top.className = "file-row-top";

    const nameEl = document.createElement("div");
    nameEl.className = "file-name";
    nameEl.textContent = item.name;
    top.appendChild(nameEl);

    const actions = document.createElement("div");
    actions.className = "file-actions";
    if (["WAITING", "TRANSFERRING", "PAUSED"].includes(item.status)) {
      const cancelBtn = document.createElement("button");
      cancelBtn.className = "mini-btn";
      cancelBtn.textContent = "Cancel";
      cancelBtn.onclick = async () => {
        await apiJson("/api/cancel-item", "POST", { id: item.id });
        refreshState();
      };
      actions.appendChild(cancelBtn);
    } else if (["FAILED", "CANCELLED"].includes(item.status) && item.direction === "SEND") {
      const retryBtn = document.createElement("button");
      retryBtn.className = "mini-btn";
      retryBtn.textContent = "Retry";
      retryBtn.onclick = async () => {
        const res = await apiJson("/api/retry-item", "POST", { id: item.id });
        if (!res.ok) showToast(res.message || "Retry unavailable");
        refreshState();
      };
      actions.appendChild(retryBtn);
    }
    top.appendChild(actions);
    row.appendChild(top);

    const track = document.createElement("div");
    track.className = "file-progress-track";
    const fill = document.createElement("div");
    fill.className = "file-progress-fill";
    if (item.status === "COMPLETED") fill.classList.add("done");
    if (item.status === "FAILED") fill.classList.add("fail");
    fill.style.width = `${Math.min(100, Math.max(0, item.progress || 0))}%`;
    track.appendChild(fill);
    row.appendChild(track);

    const meta = document.createElement("div");
    meta.className = "file-meta";
    const leftMeta = document.createElement("span");
    leftMeta.textContent = `${formatBytes(item.bytesTransferred)} / ${formatBytes(item.sizeBytes)}`;
    const rightMeta = document.createElement("span");
    rightMeta.textContent = item.errorMessage ? `${item.status}: ${item.errorMessage}` : item.status;
    meta.appendChild(leftMeta);
    meta.appendChild(rightMeta);
    row.appendChild(meta);

    return row;
  }

  function renderQrModal(state) {
    $("qr-device-name").textContent = state.deviceName || "Arc Flash PC";
    const statusPill = $("qr-status-pill");
    statusPill.textContent = state.paired ? "Paired" : "Waiting for pairing…";
    statusPill.classList.toggle("paired", !!state.paired);

    const remMs = Math.max(0, (state.qrExpiresAtMs || 0) - Date.now());
    $("qr-countdown").textContent = `Expires in ${formatDuration(remMs)}`;

    if (state.qrPayload && state.qrPayload !== lastRenderedQrPayload && window.ArcQrCode) {
      lastRenderedQrPayload = state.qrPayload;
      window.ArcQrCode.renderToCanvas($("qr-canvas"), state.qrPayload, 220);
    }
  }

  async function uploadFilesToSendQueue(fileList, category, zipMode = false, folderNameOverride = "") {
    if (!lastState?.paired) {
      showToast("Connect to a device first");
      return;
    }
    const files = Array.from(fileList || []);
    if (files.length === 0) return;

    const batchId = `pc_${Date.now()}`;
    let sentCount = 0;
    let failedCount = 0;
    let firstRootFolder = folderNameOverride;
    const eligibleFiles = [];

    for (const file of files) {
      const rel = file.webkitRelativePath && file.webkitRelativePath.length > 0 ? file.webkitRelativePath : file.name;
      if (!firstRootFolder && rel.includes("/")) {
        firstRootFolder = rel.split("/")[0];
      }
      eligibleFiles.push({ file, rel });
    }

    let nextIndex = 0;
    async function uploadWorker() {
      while (nextIndex < eligibleFiles.length) {
        const { file, rel } = eligibleFiles[nextIndex++];
        try {
          const res = await fetch("/api/send-stream", {
            method: "POST",
            headers: {
              "X-Arc-Name": encodeURIComponent(rel),
              "X-Arc-Size": String(file.size),
              "X-Arc-Batch-Id": batchId,
              "X-Arc-Category": category,
              "X-Arc-Zip-Mode": zipMode ? "1" : "0",
            },
            body: file,
          });
          const json = await res.json();
          if (!res.ok) throw new Error(json.error || `HTTP ${res.status}`);
          if (json.skipped) skippedCount++;
          else sentCount++;
        } catch (error) {
          failedCount++;
          console.error("File staging failed:", error);
        }
      }
    }
    await Promise.all(Array.from(
      { length: Math.min(6, eligibleFiles.length) },
      () => uploadWorker()
    ));

    if (zipMode && sentCount > 0) {
      const zres = await apiJson("/api/send-zip-finish", "POST", {
        batchId,
        folderName: firstRootFolder || "ArcFlash_Folder",
      });
      if (zres.ok) {
        showToast(`Queued ${firstRootFolder || "folder"}.zip (${sentCount} files)`);
      } else {
        showToast(zres.error || "ZIP packaging failed");
      }
    } else if (sentCount > 0) {
      showToast(
        failedCount > 0
          ? `Queued ${sentCount}; ${failedCount} file(s) failed to stage`
          : `Queued ${sentCount} file(s) for transfer`
      );
    } else if (failedCount > 0) {
      showToast(`${failedCount} file(s) failed to stage`);
    }
    refreshState();
  }

  // Recursively collect dropped folder files
  async function readDroppedEntries(dataTransfer) {
    const items = Array.from(dataTransfer.items || []);
    const entries = items.map((it) => it.webkitGetAsEntry && it.webkitGetAsEntry()).filter(Boolean);
    if (entries.length === 0) {
      return { files: Array.from(dataTransfer.files || []), hasDirectory: false, rootFolder: "" };
    }
    const collected = [];
    let hasDirectory = false;
    let rootFolder = "";

    async function walk(entry, prefix) {
      if (entry.isFile) {
        const f = await new Promise((resolve) => entry.file(resolve));
        Object.defineProperty(f, "webkitRelativePath", { value: prefix + entry.name });
        collected.push(f);
      } else if (entry.isDirectory) {
        hasDirectory = true;
        if (!rootFolder) rootFolder = entry.name;
        const reader = entry.createReader();
        while (true) {
          const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
          if (batch.length === 0) break;
          await Promise.all(batch.map((child) => walk(child, `${prefix}${entry.name}/`)));
        }
      }
    }

    for (const ent of entries) {
      await walk(ent, "");
    }
    return { files: collected, hasDirectory, rootFolder };
  }

  function wireEvents() {
    // Tabs
    $("tab-btn-send").onclick = () => {
      currentTab = "send";
      $("tab-btn-send").classList.add("active");
      $("tab-btn-receive").classList.remove("active");
      if (lastState) renderState(lastState);
    };
    $("tab-btn-receive").onclick = () => {
      currentTab = "receive";
      $("tab-btn-receive").classList.add("active");
      $("tab-btn-send").classList.remove("active");
      if (lastState) renderState(lastState);
    };

    // Disconnect
    $("btn-disconnect").onclick = async () => {
      await apiJson("/api/disconnect", "POST", {});
      showToast("Disconnected — transfers stopped");
      refreshState();
    };

    // Top Toolbar
    $("btn-refresh").onclick = async () => {
      $("refresh-icon").classList.add("spin");
      await apiJson("/api/refresh", "POST", {});
      setTimeout(() => $("refresh-icon").classList.remove("spin"), 600);
      showToast("Refreshed LAN discovery");
      refreshState();
    };

    $("btn-pause-resume").onclick = async () => {
      const res = await apiJson("/api/pause-resume", "POST", {});
      showToast(res.paused ? "Transfers paused on both devices" : "Transfers resumed on both devices");
      refreshState();
    };

    $("btn-stop-all").onclick = async () => {
      await apiJson("/api/stop", "POST", {});
      showToast("All transfers stopped on both devices");
      refreshState();
    };

    $("btn-clear").onclick = async () => {
      await apiJson("/api/clear", "POST", { tab: currentTab });
      showToast("Transfers cleared");
      refreshState();
    };

    // Show QR Modal
    $("btn-show-qr").onclick = () => {
      lastRenderedQrPayload = "";
      $("modal-qr").classList.remove("hidden");
      if (lastState) renderQrModal(lastState);
    };
    $("btn-qr-close").onclick = () => $("modal-qr").classList.add("hidden");
    $("btn-qr-regen").onclick = async () => {
      lastRenderedQrPayload = "";
      await apiJson("/api/qr/regenerate", "POST", {});
      showToast("QR regenerated");
      refreshState();
    };

    // Settings Modal
    $("btn-settings").onclick = () => {
      if (lastState) {
        $("input-device-name").value = lastState.deviceName || "";
        selectedSaveDir = lastState.saveDir || "";
        $("save-dir-display").textContent = selectedSaveDir;
        updateThemeChipSelection(lastState.theme || "system");
      }
      $("modal-settings").classList.remove("hidden");
    };
    $("btn-settings-close").onclick = () => $("modal-settings").classList.add("hidden");
    $("btn-pick-save-dir").onclick = async () => {
      const button = $("btn-pick-save-dir");
      button.disabled = true;
      try {
        const result = await apiJson("/api/pick-save-dir");
        if (result.ok && result.path) {
          selectedSaveDir = result.path;
          $("save-dir-display").textContent = selectedSaveDir;
        } else if (!result.cancelled) {
          showToast(result.error || "Could not open folder picker");
        }
      } catch (error) {
        showToast(`Folder picker failed: ${error.message}`);
      } finally {
        button.disabled = false;
      }
    };

    let selectedTheme = "system";
    function updateThemeChipSelection(t) {
      selectedTheme = t;
      document.querySelectorAll(".theme-chip").forEach((c) => {
        c.classList.toggle("selected", c.dataset.theme === t);
      });
    }
    document.querySelectorAll(".theme-chip").forEach((chip) => {
      chip.onclick = () => {
        updateThemeChipSelection(chip.dataset.theme);
        document.documentElement.setAttribute("data-theme", chip.dataset.theme);
      };
    });

    $("btn-settings-save").onclick = async () => {
      const res = await apiJson("/api/settings", "POST", {
        deviceName: $("input-device-name").value,
        saveDir: selectedSaveDir,
        theme: selectedTheme,
      });
      if (res.ok) {
        $("modal-settings").classList.add("hidden");
        showToast("Settings saved");
        refreshState();
      } else {
        showToast(res.message || "Failed saving settings");
      }
    };

    // Category Tiles
    document.querySelector('.cat-tile[data-cat="FILES"]').onclick = () => $("input-files").click();
    document.querySelector('.cat-tile[data-cat="FOLDERS"]').onclick = () => {
      $("modal-folder-mode").classList.remove("hidden");
    };

    $("btn-folder-zip").onclick = () => {
      folderZipMode = true;
      $("modal-folder-mode").classList.add("hidden");
      $("input-folders").click();
    };
    $("btn-folder-tree").onclick = () => {
      folderZipMode = false;
      $("modal-folder-mode").classList.add("hidden");
      $("input-folders").click();
    };
    $("btn-folder-cancel").onclick = () => $("modal-folder-mode").classList.add("hidden");

    $("input-files").onchange = (e) => { uploadFilesToSendQueue(e.target.files, "FILES"); e.target.value = ""; };
    $("input-folders").onchange = (e) => { uploadFilesToSendQueue(e.target.files, "FOLDERS", folderZipMode); e.target.value = ""; };

    // Drag & drop files or folders
    const dropzone = $("files-dropzone");
    dropzone.ondragover = (e) => {
      e.preventDefault();
      if (currentTab === "send" && lastState?.paired) dropzone.classList.add("drag-over");
    };
    dropzone.ondragleave = () => dropzone.classList.remove("drag-over");
    dropzone.ondrop = async (e) => {
      e.preventDefault();
      dropzone.classList.remove("drag-over");
      if (currentTab !== "send" || !lastState?.paired) {
        showToast("Switch to Send tab & connect to a device first");
        return;
      }
      const { files, hasDirectory, rootFolder } = await readDroppedEntries(e.dataTransfer);
      if (files.length === 0) return;
      uploadFilesToSendQueue(files, hasDirectory ? "FOLDERS" : "FILES", hasDirectory, rootFolder);
    };
  }

  wireEvents();
  refreshState();
  setInterval(() => {
    if (!$("modal-qr").classList.contains("hidden") && lastState) {
      const remMs = Math.max(0, (lastState.qrExpiresAtMs || 0) - Date.now());
      $("qr-countdown").textContent = `Expires in ${formatDuration(remMs)}`;
    }
  }, 500);
})();
