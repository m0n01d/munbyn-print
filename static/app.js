(() => {
  "use strict";

  const fileInput = document.getElementById("file-input");
  const dropZone = document.getElementById("drop-zone");
  const fileNameEl = document.getElementById("file-name");
  const sizeSelect = document.getElementById("opt-size");
  const customSizeLabels = document.querySelectorAll(".custom-size");
  const widthMm = document.getElementById("opt-width-mm");
  const heightMm = document.getElementById("opt-height-mm");
  const previewPane = document.getElementById("preview-pane");
  const resultPane = document.getElementById("result");
  const statusLine = document.getElementById("status-line");
  const busy = document.getElementById("busy");

  let currentFile = null;

  function setFile(file) {
    currentFile = file;
    fileNameEl.textContent = file ? file.name : "";
  }

  fileInput.addEventListener("change", () => {
    setFile(fileInput.files[0] || null);
  });

  ["dragover", "dragenter"].forEach((evt) => {
    dropZone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropZone.classList.add("drag");
    });
  });
  ["dragleave", "dragend", "drop"].forEach((evt) => {
    dropZone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropZone.classList.remove("drag");
    });
  });
  dropZone.addEventListener("drop", (e) => {
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) setFile(file);
  });
  dropZone.addEventListener("click", (e) => {
    if (e.target === fileInput) return;
    fileInput.click();
  });
  dropZone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInput.click();
    }
  });

  sizeSelect.addEventListener("change", () => {
    const custom = sizeSelect.value === "custom";
    customSizeLabels.forEach((el) => { el.hidden = !custom; });
  });

  function fieldValue(id) {
    const el = document.getElementById(id);
    if (!el) return "";
    if (el.type === "checkbox") return el.checked ? "1" : "";
    return el.value;
  }

  function buildFormData() {
    const fd = new FormData();
    if (currentFile) fd.append("file", currentFile, currentFile.name);

    if (sizeSelect.value === "custom") {
      const w = widthMm.value || "0";
      const h = heightMm.value || "0";
      fd.append("size", `${w}x${h}mm`);
    } else {
      fd.append("size", sizeSelect.value);
    }

    const simple = [
      ["media", "opt-media"],
      ["gap_mm", "opt-gap-mm"],
      ["gap_offset_mm", "opt-gap-offset-mm"],
      ["fit", "opt-fit"],
      ["scale", "opt-scale"],
      ["rotate", "opt-rotate"],
      ["crop", "opt-crop"],
      ["align", "opt-align"],
      ["margin_mm", "opt-margin-mm"],
      ["pages", "opt-pages"],
      ["copies", "opt-copies"],
      ["dither", "opt-dither"],
      ["threshold", "opt-threshold"],
      ["black_is_one", "opt-black-is-one"],
      ["density", "opt-density"],
      ["speed", "opt-speed"],
      ["direction", "opt-direction"],
      ["offset_mm", "opt-offset-mm"],
      ["x_shift_mm", "opt-x-shift-mm"],
      ["y_shift_mm", "opt-y-shift-mm"],
      ["feed_scale", "opt-feed-scale"],
      ["serial", "opt-serial"],
    ];
    simple.forEach(([field, id]) => {
      const v = fieldValue(id);
      if (v !== "") fd.append(field, v);
    });

    if (fieldValue("opt-invert") === "1") fd.append("invert", "1");
    return fd;
  }

  async function postFormData(url, fd) {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "X-Munbyn": "1" },
      body: fd,
    });
    let data = {};
    try {
      data = await resp.json();
    } catch (e) {
      // no/invalid JSON body
    }
    if (!resp.ok) {
      throw new Error(data.error || `request failed (HTTP ${resp.status})`);
    }
    return data;
  }

  async function postForm(url) {
    if (!currentFile) throw new Error("choose a file first");
    return postFormData(url, buildFormData());
  }

  // Same label/printer fields buildFormData() collects, without requiring a
  // chosen file -- used by the self-test buttons, which need no upload.
  function settingsFormData() {
    const fd = new FormData();
    if (sizeSelect.value === "custom") {
      const w = widthMm.value || "0";
      const h = heightMm.value || "0";
      fd.append("size", `${w}x${h}mm`);
    } else {
      fd.append("size", sizeSelect.value);
    }
    const simple = [
      ["media", "opt-media"],
      ["gap_mm", "opt-gap-mm"],
      ["gap_offset_mm", "opt-gap-offset-mm"],
      ["black_is_one", "opt-black-is-one"],
      ["density", "opt-density"],
      ["speed", "opt-speed"],
      ["direction", "opt-direction"],
      ["offset_mm", "opt-offset-mm"],
      ["x_shift_mm", "opt-x-shift-mm"],
      ["y_shift_mm", "opt-y-shift-mm"],
      ["feed_scale", "opt-feed-scale"],
      ["serial", "opt-serial"],
    ];
    simple.forEach(([field, id]) => {
      const v = fieldValue(id);
      if (v !== "") fd.append(field, v);
    });
    return fd;
  }

  function setBusy(v) {
    busy.hidden = !v;
  }

  document.getElementById("btn-preview").addEventListener("click", async () => {
    resultPane.textContent = "";
    previewPane.innerHTML = "";
    setBusy(true);
    try {
      const data = await postForm("/api/preview");
      (data.pages || []).forEach((src) => {
        const img = document.createElement("img");
        img.src = src;
        img.className = "label-preview";
        previewPane.appendChild(img);
      });
      const dims = document.createElement("p");
      dims.className = "dims";
      dims.textContent = `${data.width_dots} × ${data.height_dots} dots`;
      previewPane.appendChild(dims);
    } catch (err) {
      resultPane.textContent = `Error: ${err.message}`;
    } finally {
      setBusy(false);
    }
  });

  document.getElementById("btn-print").addEventListener("click", async () => {
    resultPane.textContent = "";
    setBusy(true);
    try {
      const data = await postForm("/api/print");
      if (data.dry_run) {
        resultPane.textContent = "Dry run (--test mode) -- nothing was sent to the printer:";
        const pre = document.createElement("pre");
        pre.textContent = data.describe;
        resultPane.appendChild(pre);
      } else {
        resultPane.textContent = `Sent ${data.pages} page(s), ${data.bytes} bytes.`;
      }
    } catch (err) {
      resultPane.textContent = `Error: ${err.message}`;
    } finally {
      setBusy(false);
    }
  });

  async function refreshStatus() {
    try {
      const resp = await fetch("/api/status", { headers: { "X-Munbyn": "1" } });
      const data = await resp.json();
      if (data.dry_run) {
        statusLine.textContent = data.status_note || "dry-run mode (--test): USB is never touched";
      } else if (data.connected) {
        const flags = data.status && data.status.length ? ` (${data.status.join(", ")})` : "";
        const note = data.status_note ? ` -- ${data.status_note}` : "";
        statusLine.textContent = `connected: ${data.device_id || "unknown device"}${flags}${note}`;
      } else {
        statusLine.textContent = "printer not found";
      }
    } catch (err) {
      statusLine.textContent = "status unavailable";
    }
  }

  document.getElementById("btn-selftest-preview").addEventListener("click", async () => {
    resultPane.textContent = "";
    previewPane.innerHTML = "";
    setBusy(true);
    try {
      const fd = settingsFormData();
      fd.append("preview", "1");
      const data = await postFormData("/api/selftest", fd);
      (data.pages || []).forEach((src) => {
        const img = document.createElement("img");
        img.src = src;
        img.className = "label-preview";
        previewPane.appendChild(img);
      });
      const dims = document.createElement("p");
      dims.className = "dims";
      dims.textContent = `${data.width_dots} × ${data.height_dots} dots`;
      previewPane.appendChild(dims);
    } catch (err) {
      resultPane.textContent = `Error: ${err.message}`;
    } finally {
      setBusy(false);
    }
  });

  document.getElementById("btn-selftest-print").addEventListener("click", async () => {
    resultPane.textContent = "";
    setBusy(true);
    try {
      const data = await postFormData("/api/selftest", settingsFormData());
      if (data.dry_run) {
        resultPane.textContent = "Dry run (--test mode) -- nothing was sent to the printer:";
        const pre = document.createElement("pre");
        pre.textContent = data.describe;
        resultPane.appendChild(pre);
      } else {
        resultPane.textContent = `Self-test sent (${data.bytes} bytes).`;
      }
    } catch (err) {
      resultPane.textContent = `Error: ${err.message}`;
    } finally {
      setBusy(false);
    }
  });

  document.getElementById("btn-scaletest-preview").addEventListener("click", async () => {
    resultPane.textContent = "";
    previewPane.innerHTML = "";
    setBusy(true);
    try {
      const fd = settingsFormData();
      fd.append("preview", "1");
      const data = await postFormData("/api/scale-test", fd);
      (data.pages || []).forEach((src) => {
        const img = document.createElement("img");
        img.src = src;
        img.className = "label-preview";
        previewPane.appendChild(img);
      });
      const dims = document.createElement("p");
      dims.className = "dims";
      dims.textContent = `${data.width_dots} × ${data.height_dots} dots`;
      previewPane.appendChild(dims);
    } catch (err) {
      resultPane.textContent = `Error: ${err.message}`;
    } finally {
      setBusy(false);
    }
  });

  document.getElementById("btn-scaletest-print").addEventListener("click", async () => {
    resultPane.textContent = "";
    setBusy(true);
    try {
      const data = await postFormData("/api/scale-test", settingsFormData());
      if (data.dry_run) {
        resultPane.textContent = "Dry run (--test mode) -- nothing was sent to the printer:";
        const pre = document.createElement("pre");
        pre.textContent = data.describe;
        resultPane.appendChild(pre);
      } else {
        resultPane.textContent = `Scale-test sent (${data.bytes} bytes).`;
      }
    } catch (err) {
      resultPane.textContent = `Error: ${err.message}`;
    } finally {
      setBusy(false);
    }
  });

  refreshStatus();
})();
