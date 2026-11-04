// View contract shared with web/mainsail/arm_control_panel.js: same poll
// cadence (700 ms), same /api/* endpoints, same state-color tokens.
(() => {
  "use strict";

  const token = document.querySelector('meta[name="arm-token"]').content;
  const state = {
    status: null,
    polling: false,
    timer: null,
    toastTimer: null,
    lastConnect: 0,
    recovering: false,
  };

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-Arm-Token": token,
        ...(options.headers || {}),
      },
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      throw new Error(`HTTP ${response.status}`);
    }
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    return payload;
  }

  function showToast(message) {
    const toast = document.getElementById("toast");
    clearTimeout(state.toastTimer);
    toast.textContent = message || "";
    toast.hidden = !message;
    if (message) {
      state.toastTimer = setTimeout(() => {
        toast.hidden = true;
      }, 4500);
    }
  }

  function format(value, axis) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return "—";
    }
    const digits = axis.kind === "rotary" ? 1 : 2;
    return Number(Number(value).toFixed(digits)).toString();
  }

  function render(status) {
    state.status = status;
    const led = document.getElementById("link-led");
    const ready = status.connected && status.klippy_state === "ready";
    const alarm =
      status.estopped ||
      status.faulted ||
      status.klippy_state === "shutdown";
    led.className = `link-led ${alarm ? "is-alarm" : ready ? "is-ready" : ""}`;
    led.setAttribute(
      "aria-label",
      alarm ? "Emergency stop latched" : ready ? "Connected" : "Offline"
    );
    document.body.dataset.estopped = status.estopped ? "true" : "false";

    const stateText = document.getElementById("stack-state");
    const detail = document.getElementById("stack-detail");
    const recover = document.getElementById("recover");
    const klippy = status.connected ? status.klippy_state : "offline";
    stateText.textContent = `klipper: ${klippy}`;
    stateText.classList.toggle("is-ready", ready && !alarm);
    stateText.classList.toggle("is-alarm", alarm || Boolean(status.faulted));
    // RECOVER appears whenever klippy cannot run motion: e-stop latch,
    // shutdown state, or an unresolved fault. controller.restart() handles
    // all three (firmware_restart + wait_ready + latch clear).
    recover.hidden = !alarm;
    if (status.faulted && status.fault_reason) {
      detail.textContent = status.fault_reason;
      detail.hidden = false;
    } else if (status.estopped) {
      detail.textContent = "estop latched; RECOVER restarts klipper";
      detail.hidden = false;
    } else if (alarm) {
      detail.textContent = "klippy down; RECOVER restarts it";
      detail.hidden = false;
    } else {
      detail.hidden = true;
    }

    for (const axis of ["m1", "m2", "m3"]) {
      const value = document.getElementById(`${axis}-value`);
      value.textContent = format(status.positions[axis], status.axes[axis]);
      value.closest(".axis").dataset.homed = String(Boolean(status.homed[axis]));
    }
  }

  async function refresh() {
    if (state.polling) return;
    state.polling = true;
    try {
      let payload = await api("/api/status");
      if (
        !payload.status.connected &&
        Date.now() - state.lastConnect > 2500
      ) {
        state.lastConnect = Date.now();
        try {
          payload = await api("/api/connect", { method: "POST" });
        } catch {
          // The LED and the next poll handle an unavailable Moonraker.
        }
      }
      render(payload.status);
    } catch (error) {
      showToast(error.message);
    } finally {
      state.polling = false;
      clearTimeout(state.timer);
      state.timer = setTimeout(refresh, 700);
    }
  }

  async function jog(axis, delta) {
    try {
      const payload = await api("/api/jog", {
        method: "POST",
        body: JSON.stringify({ axis, delta }),
      });
      render(payload.status);
    } catch (error) {
      showToast(error.message);
    }
  }

  async function recover() {
    if (state.recovering) return;
    state.recovering = true;
    showToast("recovering klipper…");
    try {
      // Blocks up to ~90s while controller.restart() waits for klippy.
      const payload = await api("/api/restart", { method: "POST" });
      showToast("klipper ready");
      render(payload.status);
    } catch (error) {
      showToast(error.message);
    } finally {
      state.recovering = false;
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-jog-axis]");
    if (button) {
      jog(button.dataset.jogAxis, Number(button.dataset.delta));
      return;
    }

    if (event.target.closest("#home")) {
      api("/api/home", {
        method: "POST",
        body: JSON.stringify({ axis: "all" }),
      })
        .then((payload) => {
          showToast("homed");
          render(payload.status);
        })
        .catch((error) => showToast(error.message));
      return;
    }

    if (event.target.closest("#recover")) {
      recover();
      return;
    }

    if (event.target.closest("#estop")) {
      api("/api/estop", { method: "POST" })
        .then((payload) => {
          showToast("");
          render(payload.status);
        })
        .catch((error) => showToast(error.message));
    }
  });

  refresh();
})();
