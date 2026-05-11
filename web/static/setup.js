// First-run wizard logic — vanilla JS, no build step.
(() => {
  const form = document.getElementById("setup-form");
  const pw = document.getElementById("password");
  const confirm = document.getElementById("confirm");
  const strength = document.getElementById("strength");
  const match = document.getElementById("match");
  const errorBox = document.getElementById("error");
  const submit = document.getElementById("submit");
  const testBtn = document.getElementById("test-btn");
  const testResult = document.getElementById("test-result");
  const address = document.getElementById("address");

  function update() {
    const len = pw.value.length;
    if (len === 0) { strength.textContent = ""; strength.className = "hint"; }
    else if (len < 8) { strength.textContent = `${len}/8 characters`; strength.className = "hint bad"; }
    else { strength.textContent = "OK"; strength.className = "hint ok"; }

    if (confirm.value === "") { match.textContent = ""; match.className = "hint"; }
    else if (confirm.value !== pw.value) { match.textContent = "Passwords don't match"; match.className = "hint bad"; }
    else { match.textContent = "Match"; match.className = "hint ok"; }

    submit.disabled = !(len >= 8 && confirm.value === pw.value);
  }

  pw.addEventListener("input", update);
  confirm.addEventListener("input", update);

  testBtn.addEventListener("click", async () => {
    const v = address.value.trim();
    if (!v) { testResult.textContent = "Enter an address first"; testResult.className = "hint bad"; return; }
    testResult.textContent = "Testing…"; testResult.className = "hint";
    try {
      const r = await fetch("/api/setup/test-dashcam", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address: v }),
      });
      const j = await r.json();
      if (j.ok) { testResult.textContent = `Reachable (${j.latency_ms}ms)`; testResult.className = "hint ok"; }
      else { testResult.textContent = `Failed: ${j.error}`; testResult.className = "hint bad"; }
    } catch (e) {
      testResult.textContent = `Error: ${e.message}`;
      testResult.className = "hint bad";
    }
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    errorBox.hidden = true;
    const fd = new FormData(form);
    const r = await fetch("/setup", { method: "POST", body: fd, redirect: "manual" });
    if (r.status === 303 || r.type === "opaqueredirect") {
      window.location.href = "/";
    } else {
      const text = await r.text();
      errorBox.hidden = false;
      errorBox.textContent = text || `Setup failed (${r.status})`;
    }
  });

  update();
})();
