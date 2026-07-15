// NewsIntel — client-side interactivity
// - Category filter chips (client-side only, data never changes post-render)
// - Advanced options panel toggle + conditional date fields
// - Search form submission -> job kickoff -> polling -> reload on success
// - Past-runs picker

(function () {
  "use strict";

  // ---- Category filter chips -------------------------------------------
  const chips = document.querySelectorAll(".category-filters__chip");
  const cards = document.querySelectorAll(".article-card");
  chips.forEach((chip) => {
    chip.addEventListener("click", () => {
      const filter = chip.dataset.filter;
      chips.forEach((c) => c.classList.remove("is-active"));
      chip.classList.add("is-active");
      cards.forEach((card) => {
        const match = filter === "all" || card.dataset.category === filter;
        card.classList.toggle("is-hidden", !match);
      });
    });
  });

  // ---- Advanced options panel --------------------------------------------
  const advancedToggle = document.getElementById("advanced-toggle");
  const advancedPanel = document.getElementById("advanced-panel");
  if (advancedToggle && advancedPanel) {
    advancedToggle.addEventListener("click", () => {
      const isOpen = !advancedPanel.hidden;
      advancedPanel.hidden = isOpen;
      advancedToggle.setAttribute("aria-expanded", String(!isOpen));
    });
  }

  const dateMode = document.getElementById("date-mode");
  const sinceField = document.getElementById("since-field");
  const fromField = document.getElementById("from-field");
  const toField = document.getElementById("to-field");
  if (dateMode) {
    dateMode.addEventListener("change", () => {
      const isRange = dateMode.value === "range";
      sinceField.hidden = isRange;
      fromField.hidden = !isRange;
      toField.hidden = !isRange;
    });
  }

  // ---- Runs picker --------------------------------------------------------
  const runsSelect = document.getElementById("runs-select");
  if (runsSelect) {
    runsSelect.addEventListener("change", async () => {
      const path = runsSelect.value;
      if (!path) return;
      try {
        const res = await fetch("/api/runs/load", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Could not load that run.");
        window.location.reload();
      } catch (err) {
        alert(err.message);
      }
    });
  }

  // ---- Search form / job pipeline ----------------------------------------
  const form = document.getElementById("search-form");
  const submitBtn = document.getElementById("search-submit");
  const overlay = document.getElementById("progress-overlay");
  const progressTitle = document.getElementById("progress-title");
  const progressSubtitle = document.getElementById("progress-subtitle");
  const progressLog = document.getElementById("progress-log");
  const progressError = document.getElementById("progress-error");
  const progressErrorStep = document.getElementById("progress-error-step");
  const progressErrorMessage = document.getElementById("progress-error-message");
  const progressDismiss = document.getElementById("progress-dismiss");
  const stepFetch = document.getElementById("step-fetch");
  const stepClassify = document.getElementById("step-classify");
  const stepDone = document.getElementById("step-done");

  const STEP_LABELS = {
    queued: "Starting up…",
    fetching: "Fetching articles from sources…",
    classifying: "Classifying articles…",
    done: "Done",
    error: "Something went wrong",
  };

  function resetProgressUI() {
    [stepFetch, stepClassify, stepDone].forEach((el) =>
      el.classList.remove("is-active", "is-complete", "is-error")
    );
    progressLog.textContent = "";
    progressError.hidden = true;
    progressDismiss.hidden = true;
    progressSubtitle.textContent = "This can take a little while depending on sources.";
  }

  function applyStepUI(status) {
    if (status === "queued" || status === "fetching") {
      stepFetch.classList.add("is-active");
    } else if (status === "classifying") {
      stepFetch.classList.remove("is-active");
      stepFetch.classList.add("is-complete");
      stepClassify.classList.add("is-active");
    } else if (status === "done") {
      stepFetch.classList.remove("is-active");
      stepFetch.classList.add("is-complete");
      stepClassify.classList.remove("is-active");
      stepClassify.classList.add("is-complete");
      stepDone.classList.add("is-complete");
    } else if (status === "error") {
      [stepFetch, stepClassify].forEach((el) => {
        if (el.classList.contains("is-active")) {
          el.classList.remove("is-active");
          el.classList.add("is-error");
        }
      });
    }
  }

  function collectFormData() {
    const query = document.getElementById("query-input").value.trim();
    const dateModeVal = dateMode ? dateMode.value : "since";
    const sourceCheckboxes = Array.from(
      document.querySelectorAll('input[name="source"]:checked')
    ).map((cb) => cb.value);

    const payload = {
      query,
      date_mode: dateModeVal,
      sources: sourceCheckboxes.length ? sourceCheckboxes : ["all"],
    };

    if (dateModeVal === "range") {
      payload.from_date = document.getElementById("from-date").value || null;
      payload.to_date = document.getElementById("to-date").value || null;
    } else {
      payload.since = document.getElementById("since-input").value;
    }

    const noDedup = document.getElementById("no-dedup");
    if (noDedup && noDedup.checked) {
      payload.no_dedup = true;
    } else {
      const threshold = document.getElementById("dedup-threshold");
      if (threshold && threshold.value) payload.dedup_threshold = Number(threshold.value);
    }

    const noScrape = document.getElementById("no-scrape");
    if (noScrape && noScrape.checked) payload.no_scrape_fallback = true;

    const provider = document.getElementById("provider");
    if (provider && provider.value) payload.provider = provider.value;

    return payload;
  }

  async function pollJob(jobId) {
    const maxAttempts = 600; // ~10 min at 1s interval, generous ceiling
    let attempts = 0;

    return new Promise((resolve, reject) => {
      const interval = setInterval(async () => {
        attempts += 1;
        if (attempts > maxAttempts) {
          clearInterval(interval);
          reject(new Error("Timed out waiting for the pipeline to finish."));
          return;
        }
        try {
          const res = await fetch(`/api/jobs/${jobId}`);
          const job = await res.json();
          if (!res.ok) throw new Error(job.error || "Lost track of the job.");

          progressTitle.textContent = STEP_LABELS[job.status] || job.step_label;
          applyStepUI(job.status);
          if (job.log && job.log.length) {
            progressLog.textContent = job.log.slice(-30).join("\n");
            progressLog.scrollTop = progressLog.scrollHeight;
          }

          if (job.status === "done") {
            clearInterval(interval);
            resolve(job);
          } else if (job.status === "error") {
            clearInterval(interval);
            reject(Object.assign(new Error(job.error?.message || "Pipeline failed."), {
              step: job.error?.step,
            }));
          }
        } catch (err) {
          clearInterval(interval);
          reject(err);
        }
      }, 1000);
    });
  }

  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const payload = collectFormData();
      if (!payload.query) return;

      resetProgressUI();
      overlay.hidden = false;
      submitBtn.disabled = true;
      progressTitle.textContent = "Starting up…";

      try {
        const res = await fetch("/api/search", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Could not start the search.");

        const job = await pollJob(data.job_id);

        // Make this run's output the active dataset, then reload.
        const loadRes = await fetch(`/api/jobs/${job.id}/load`, { method: "POST" });
        const loadData = await loadRes.json();
        if (!loadRes.ok) throw new Error(loadData.error || "Could not load the results.");

        progressTitle.textContent = "Done — loading results…";
        window.location.reload();
      } catch (err) {
        progressTitle.textContent = "Something went wrong";
        progressSubtitle.textContent = "";
        progressError.hidden = false;
        progressErrorStep.textContent = err.step ? `Failed during: ${err.step}` : "Failed";
        progressErrorMessage.textContent = err.message;
        progressDismiss.hidden = false;
        submitBtn.disabled = false;
      }
    });
  }

  if (progressDismiss) {
    progressDismiss.addEventListener("click", () => {
      overlay.hidden = true;
      submitBtn.disabled = false;
    });
  }
})();
