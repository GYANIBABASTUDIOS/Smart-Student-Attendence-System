(() => {
  const startButton = document.getElementById("recognition-start");
  const panel = document.getElementById("teacher-recognition-panel");
  if (!startButton || !panel) return;

  const message = document.getElementById("teacher-recognition-message");
  const faceList = document.getElementById("teacher-recognition-faces");
  const video = document.getElementById("teacher-recognition-video");
  const placeholder = document.getElementById("teacher-recognition-placeholder");
  const csrf = startButton.dataset.csrf;
  const confirmedSeen = new Set();
  let running = panel.dataset.running === "true";
  let pollTimer = null;
  let polling = false;

  function setRunning(value) {
    running = value;
    startButton.textContent = value ? "Stop face recognition" : "Start face recognition";
    startButton.classList.toggle("danger", value);
    video.hidden = !value;
    placeholder.hidden = value;
    if (value) {
      video.src = `${startButton.dataset.feedUrl}?t=${Date.now()}`;
      pollTimer = window.setInterval(poll, 1200);
      poll();
    } else {
      if (pollTimer) window.clearInterval(pollTimer);
      pollTimer = null;
      video.removeAttribute("src");
      faceList.replaceChildren();
    }
  }

  async function post(url, body = "") {
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-CSRFToken": csrf,
      },
      body,
      credentials: "same-origin",
    });
  }

  async function poll() {
    if (!running || polling) return;
    polling = true;
    try {
      const response = await fetch(startButton.dataset.faceUrl, { credentials: "same-origin" });
      const data = await response.json();
      if (!response.ok) {
        if (response.status === 403 || response.status === 409) {
          try { await post(startButton.dataset.stopUrl); } catch (_) { /* owner may have changed */ }
          setRunning(false);
        }
        throw new Error(data.message || "Could not read recognition results.");
      }
      faceList.replaceChildren();
      const newlyConfirmed = [];
      for (const face of data.faces || []) {
        const item = document.createElement("li");
        if (face.confirmed && face.student_id !== null && face.student_id !== undefined) {
          item.textContent = `${face.name} (${face.student_roll}) — confirmed`;
          if (!confirmedSeen.has(face.student_id)) newlyConfirmed.push(face.student_id);
        } else if (face.student_id !== null && face.student_id !== undefined) {
          item.textContent = `${face.name} — checking ${face.streak} / ${data.confirm_frames || "required"}`;
        } else {
          item.textContent = "Unknown face — no attendance marked";
        }
        faceList.append(item);
      }
      if (newlyConfirmed.length) {
        const marked = await post(startButton.dataset.markUrl);
        const result = await marked.json();
        if (!marked.ok) {
          if (marked.status === 403 || marked.status === 409) {
            try { await post(startButton.dataset.stopUrl); } catch (_) { /* owner may have changed */ }
            setRunning(false);
          }
          throw new Error(result.message || "Could not record confirmed faces.");
        }
        newlyConfirmed.forEach((id) => confirmedSeen.add(id));
        const names = (result.marked_students || []).map((row) => row.message);
        message.textContent = names.length ? names.join(" · ") : "Confirmed face was already marked or no longer matches this class.";
      }
    } catch (error) {
      message.textContent = error.message || "Recognition polling failed.";
    } finally {
      polling = false;
    }
  }

  startButton.addEventListener("click", async () => {
    startButton.disabled = true;
    try {
      if (running) {
        const response = await post(startButton.dataset.stopUrl);
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || "Unable to stop recognition.");
        message.textContent = data.message;
        setRunning(false);
        return;
      }
      confirmedSeen.clear();
      const body = new URLSearchParams({ class_section_id: startButton.dataset.classId });
      const response = await post(startButton.dataset.startUrl, body.toString());
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || "Unable to start recognition.");
      message.textContent = data.message;
      setRunning(true);
    } catch (error) {
      message.textContent = error.message || "Recognition request failed.";
    } finally {
      startButton.disabled = false;
    }
  });

  if (running) setRunning(true);
})();
