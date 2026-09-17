/* PII Remover - front end. No framework, no external requests, no telemetry. */
(function () {
  "use strict";

  const CFG = window.PII_CONFIG || {};
  const ROOT = CFG.rootPath || "";

  const $ = (id) => document.getElementById(id);
  const el = {
    dropzone: $("dropzone"),
    fileInput: $("file-input"),
    fileMeta: $("file-meta"),
    fileName: $("file-name"),
    fileSize: $("file-size"),
    clearFile: $("clear-file"),
    runBtn: $("run-btn"),
    runHint: $("run-hint"),
    progress: $("progress"),
    barFill: $("bar-fill"),
    progressText: $("progress-text"),
    errorBox: $("error-box"),
    results: $("results-panel"),
    cleanedOut: $("cleaned-output"),
    cleanedStats: $("cleaned-stats"),
    badgeCleaned: $("badge-cleaned"),
    audit: $("audit-body"),
    jobMeta: $("job-meta"),
    archRuntime: $("arch-runtime"),
  };

  let selectedFile = null;
  let currentJob = null;
  let pollTimer = null;

  /* ------------------------------------------------------------ utilities */
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  const fmtBytes = (n) => {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  };

  const show = (node, on) => node.classList.toggle("hidden", !on);

  function fail(message) {
    el.errorBox.textContent = message;
    show(el.errorBox, true);
    show(el.progress, false);
    el.runBtn.disabled = !selectedFile;
  }

  function clearError() {
    el.errorBox.textContent = "";
    show(el.errorBox, false);
  }

  /* --------------------------------------------------------------- upload */
  el.dropzone.addEventListener("click", () => el.fileInput.click());
  el.dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      el.fileInput.click();
    }
  });

  ["dragenter", "dragover"].forEach((evt) =>
    el.dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      el.dropzone.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    el.dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      el.dropzone.classList.remove("dragover");
    })
  );
  el.dropzone.addEventListener("drop", (e) => {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length) pickFile(files[0]);
  });

  el.fileInput.addEventListener("change", () => {
    if (el.fileInput.files.length) pickFile(el.fileInput.files[0]);
  });

  el.clearFile.addEventListener("click", () => {
    selectedFile = null;
    el.fileInput.value = "";
    show(el.fileMeta, false);
    el.runBtn.disabled = true;
    clearError();
  });

  function pickFile(file) {
    const maxBytes = (CFG.maxUploadMb || 25) * 1024 * 1024;
    clearError();
    if (file.size > maxBytes) {
      fail(
        "File is " + fmtBytes(file.size) + ", which exceeds the " +
        (CFG.maxUploadMb || 25) + " MB limit."
      );
      return;
    }
    selectedFile = file;
    el.fileName.textContent = file.name;
    el.fileSize.textContent = fmtBytes(file.size);
    show(el.fileMeta, true);
    el.runBtn.disabled = false;
  }

  /* ------------------------------------------------------------ pipeline */
  el.runBtn.addEventListener("click", runPipeline);

  async function runPipeline() {
    if (!selectedFile) return;
    clearError();
    show(el.results, false);
    show(el.progress, true);
    el.runBtn.disabled = true;
    el.runHint.classList.add("hidden");
    setProgress(6, "uploading " + selectedFile.name + "…");

    const body = new FormData();
    body.append("file", selectedFile, selectedFile.name);

    let upload;
    try {
      upload = await fetch(ROOT + "/api/upload", { method: "POST", body });
    } catch (err) {
      return fail("Upload failed: " + err.message);
    }
    if (!upload.ok) {
      const detail = await upload.text();
      return fail("Upload rejected (" + upload.status + "):\n" + detail);
    }
    const job = await upload.json();
    currentJob = job.job_id;
    setProgress(14, "queued as " + job.job_id + " · extracting…");
    pollJob(job.job_id, 0);
  }

  function setProgress(pct, text) {
    el.barFill.style.width = Math.max(6, Math.min(100, pct)) + "%";
    el.progressText.textContent = text;
  }

  function pollJob(jobId, attempt) {
    clearTimeout(pollTimer);
    const stages = [
      "extracting text (visual LLM / text layer)…",
      "running deterministic validators…",
      "detector agent: enumerating PII…",
      "critic agent: checking the detection…",
      "masking agreed findings…",
    ];
    const pct = Math.min(92, 16 + attempt * 6);
    setProgress(pct, stages[Math.min(stages.length - 1, Math.floor(attempt / 3))]);

    fetch(ROOT + "/api/jobs/" + encodeURIComponent(jobId), { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error("job lookup failed (" + r.status + ")");
        return r.json();
      })
      .then((data) => {
        if (data.status === "done") {
          setProgress(100, "done · " + (data.trace && data.trace.duration_seconds
            ? data.trace.duration_seconds + " s" : "complete"));
          setTimeout(() => show(el.progress, false), 600);
          renderResult(data);
          el.runBtn.disabled = false;
          el.runHint.classList.remove("hidden");
          return;
        }
        if (data.status === "error") {
          return fail("Pipeline error:\n" + (data.error || "unknown error"));
        }
        if (attempt > 240) {
          return fail("Timed out waiting for the job to finish.");
        }
        pollTimer = setTimeout(() => pollJob(jobId, attempt + 1), 1200);
      })
      .catch((err) => fail("Lost contact with the server: " + err.message));
  }

  /* -------------------------------------------------------------- results */
  function renderResult(data) {
    currentJob = data.job_id;
    const masking = data.masking || {};
    const extraction = data.extraction || {};
    const trace = data.trace || {};

    el.cleanedOut.textContent = masking.cleaned_text || "(empty)";
    const masked = masking.total_masked || 0;
    const bytes = (masking.cleaned_text || "").length;
    el.badgeCleaned.textContent = masked + " masked";
    el.cleanedStats.textContent =
      bytes.toLocaleString() + " characters · " + masked + " distinct value(s) replaced";

    el.jobMeta.textContent =
      "job " + data.job_id.slice(0, 14) + "… · " + (trace.duration_seconds || "?") + " s · " +
      "extraction: " + (extraction.extraction_method || "n/a");

    renderAudit(data);
    show(el.results, true);
    el.results.scrollIntoView({ behavior: "smooth", block: "start" });

    document.querySelectorAll("[data-download]").forEach((btn) => {
      btn.onclick = () => {
        window.location.href =
          ROOT + "/api/jobs/" + encodeURIComponent(currentJob) +
          "/download?kind=" + encodeURIComponent(btn.dataset.download);
      };
    });
    document.querySelectorAll("[data-copy]").forEach((btn) => {
      btn.onclick = async () => {
        const which = btn.dataset.copy;
        const text = masking.cleaned_text || "";
        try {
          await navigator.clipboard.writeText(text);
          const original = btn.textContent;
          btn.textContent = "Copied";
          setTimeout(() => (btn.textContent = original), 1200);
        } catch (err) {
          btn.textContent = "Copy failed";
        }
      };
    });
  }

  function renderAudit(data) {
    const masking = data.masking || {};
    const extraction = data.extraction || {};
    const byKind = masking.by_kind || {};
    const rounds = data.detections_rounds || [];
    const warnings = [].concat(extraction.warnings || []);

    let html = "";
    html += "<h3>Masked categories</h3>";
    const kinds = Object.keys(byKind);
    if (kinds.length) {
      html += '<table class="audit"><thead><tr><th>Category</th><th>Occurrences</th></tr></thead><tbody>';
      kinds
        .sort((a, b) => byKind[b] - byKind[a])
        .forEach((k) => {
          html += "<tr><td class='mono'>" + esc(k) + "</td><td>" + byKind[k] + "</td></tr>";
        });
      html += "</tbody></table>";
    } else {
      html += "<p class='muted'>No PII categories were matched.</p>";
    }

    html += "<h3>Redaction table</h3>";
    const changes = masking.changes || [];
    if (changes.length) {
      html +=
        '<table class="audit"><thead><tr><th>Redaction</th><th>Type</th>' +
        "<th>Original (masked)</th></tr></thead><tbody>";
      changes
        .slice()
        .sort((a, b) => {
          const k = (a.kind || "").localeCompare(b.kind || "");
          return k !== 0 ? k : (a.placeholder || "").localeCompare(b.placeholder || "");
        })
        .forEach((c) => {
          html +=
            "<tr><td class='mono'>" + esc(c.placeholder || "\u2014") +
            "</td><td class='mono'>" + esc(c.kind || "OTHER") +
            "</td><td class='mono'>" + esc(c.original || "\u2014") + "</td></tr>";
        });
      html += "</tbody></table>";
      html +=
        "<p class='muted'>Placeholders are stable per deployment: the same value always " +
        "maps to the same token and cannot be reversed. Originals are shown as a masked " +
        "preview so this table stays safe to share.</p>";
    } else {
      html += "<p class='muted'>No redactions were applied.</p>";
    }

    html += "<h3>Detector / critic rounds</h3>";
    if (rounds.length) {
      html +=
        '<table class="audit"><thead><tr><th>Round</th><th>Findings</th><th>Agreement</th>' +
        "<th>Missing</th><th>False positives</th><th>Accepted</th></tr></thead><tbody>";
      rounds.forEach((r) => {
        html +=
          "<tr><td>" + r.round + "</td><td>" + r.detector_findings + "</td><td>" +
          (r.critic_agreement != null ? Number(r.critic_agreement).toFixed(2) : "&mdash;") +
          "</td><td>" + r.critic_missing + "</td><td>" + r.critic_false_positives +
          "</td><td>" + (r.accepted ? "yes" : "no") + "</td></tr>";
      });
      html += "</tbody></table>";
    } else {
      html += "<p class='muted'>No rounds recorded.</p>";
    }

    const residual = (masking.residual_findings || []).filter((r) => r && r.value);
    if (residual.length) {
      html += "<h3>Residual findings reported by the masker</h3><ul>";
      residual.forEach((r) => {
        html += "<li><code>" + esc(r.kind || "OTHER") + "</code> &mdash; " + esc(r.reason || "") + "</li>";
      });
      html += "</ul>";
    }

    html += "<h3>Extraction</h3>";
    html +=
      "<p class='muted'>kind: <code>" + esc(extraction.kind) + "</code> · method: <code>" +
      esc(extraction.extraction_method) + "</code> · pages: " + (extraction.page_count || 1) +
      " · " + (extraction.char_count || 0).toLocaleString() + " characters</p>";

    if (warnings.length) {
      html += "<h3>Warnings</h3><ul>";
      warnings.forEach((w) => (html += "<li>" + esc(w) + "</li>"));
      html += "</ul>";
    }

    html += "<h3>Encryption at rest</h3>";
    const enc = data.encryption || {};
    html += enc.encrypted_at_rest
      ? "<p class='muted'>Artifacts written encrypted (Fernet / AES-128-CBC + HMAC). " +
        "The raw upload was deleted after processing.</p>"
      : "<p class='muted'>Artifact encryption is DISABLED for this run.</p>";

    el.audit.innerHTML = html;
  }

  /* --------------------------------------------------------------- runtime */
  function loadRuntime() {
    fetch(ROOT + "/api/health", { cache: "no-store" })
      .then((r) => r.json())
      .then((h) => {
        el.archRuntime.innerHTML =
          "<span class='chip'>status: " + esc(h.status) + "</span>" +
          "<span class='chip'>uptime: " + esc(h.uptime_seconds) + " s</span>" +
          "<span class='chip'>encryption: " + (h.encryption_at_rest ? "on" : "off") + "</span>" +
          "<span class='chip'>retention: " + esc(h.retention_minutes) + " min</span>";
      })
      .catch(() => {});

    fetch(ROOT + "/api/architecture", { cache: "no-store" })
      .then((r) => r.json())
      .then((a) => {
        el.archRuntime.insertAdjacentHTML(
          "beforeend",
          a.agents
            .map((ag) => "<span class='chip'>agent " + esc(ag.name) + ": " + esc(ag.model) + "</span>")
            .join("")
        );
      })
      .catch(() => {});
  }

  loadRuntime();
})();
