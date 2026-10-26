(() => {
  "use strict";

  const token = document.querySelector('meta[name="arm-token"]').content;
  const $ = (id) => document.getElementById(id);
  const state = {
    status: null,
    polling: false,
    timer: null,
    toastTimer: null,
    lastError: "",
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
    const toast = $("toast");
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

  function canUse(axes) {
    if (!state.status || !state.status.connected || state.status.estopped) {
      return false;
    }
    return axes.every(([axis]) => Boolean(state.status.homed[axis]));
  }

  function jogDelta(axisName, normalized) {
    const magnitude = Math.abs(normalized);
    const deadzone = 0.18;
    if (magnitude < deadzone) return null;
    const axis = state.status.axes[axisName];
    const t = (magnitude - deadzone) / (1 - deadzone);
    const step =
      axis.kind === "rotary" ? 0.5 + 3.5 * t : 0.08 + 0.72 * t;
    const digits = axis.kind === "rotary" ? 1 : 2;
    const delta = Number((Math.sign(normalized) * step).toFixed(digits));
    return delta === 0 ? null : delta;
  }

  async function sendJog(axis, normalized) {
    const delta = jogDelta(axis, normalized);
    if (delta === null) return true;
    try {
      const payload = await api("/api/jog", {
        method: "POST",
        body: JSON.stringify({ axis, delta }),
      });
      render(payload.status);
      return true;
    } catch (error) {
      showToast(error.message);
      return false;
    }
  }

  function setupStick(element, axes) {
    const knob = element.querySelector(".stick-knob");
    const motion = {
      pointerId: null,
      active: false,
      loop: null,
      x: 0,
      y: 0,
    };

    function resetKnob() {
      motion.x = 0;
      motion.y = 0;
      knob.style.transform = "translate(-50%, -50%)";
    }

    function updateFromPointer(event) {
      const rect = element.getBoundingClientRect();
      const radius = rect.width * 0.31;
      let dx = event.clientX - (rect.left + rect.width / 2);
      let dy = event.clientY - (rect.top + rect.height / 2);
      const length = Math.hypot(dx, dy);
      if (length > radius) {
        dx = (dx / length) * radius;
        dy = (dy / length) * radius;
      }
      motion.x = dx / radius;
      motion.y = dy / radius;
      knob.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`;
    }

    function stop() {
      motion.active = false;
      motion.pointerId = null;
      clearTimeout(motion.loop);
      motion.loop = null;
      resetKnob();
    }

    async function tick() {
      motion.loop = null;
      if (!motion.active) return;
      if (!canUse(axes)) {
        stop();
        return;
      }
      for (const [axis, component] of axes) {
        const value = component === "x" ? motion.x : motion.y;
        if (!(await sendJog(axis, value))) {
          stop();
          return;
        }
      }
      if (motion.active) {
        motion.loop = setTimeout(tick, 120);
      }
    }

    element.addEventListener("pointerdown", (event) => {
      if (!canUse(axes)) return;
      event.preventDefault();
      motion.pointerId = event.pointerId;
      element.setPointerCapture(event.pointerId);
      updateFromPointer(event);
      motion.active = true;
      tick();
    });

    element.addEventListener("pointermove", (event) => {
      if (!motion.active || event.pointerId !== motion.pointerId) return;
      event.preventDefault();
      updateFromPointer(event);
    });

    for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
      element.addEventListener(type, stop);
    }

    element.addEventListener("keydown", async (event) => {
      const directions = {
        ArrowLeft: [-1, 0],
        ArrowRight: [1, 0],
        ArrowUp: [0, -1],
        ArrowDown: [0, 1],
      };
      const direction = directions[event.key];
      if (!direction || !canUse(axes)) return;
      event.preventDefault();
      for (const [axis, component] of axes) {
        const value = component === "x" ? direction[0] : direction[1];
        if (value) await sendJog(axis, value);
      }
    });

    element.addEventListener("contextmenu", (event) => event.preventDefault());
    window.addEventListener("blur", stop);
  }

  function render(status) {
    state.status = status;
    const led = $("link-led");
    const ready = status.connected && status.klippy_state === "ready";
    const alarm = status.estopped || status.klippy_state === "shutdown";
    led.className = `link-led ${alarm ? "is-alarm" : ready ? "is-ready" : ""}`;
    led.setAttribute(
      "aria-label",
      alarm
        ? "Emergency stop latched"
        : ready
          ? "Connected and ready"
          : "Offline"
    );
    document.body.dataset.estopped = status.estopped ? "true" : "false";

    const values = {
      m1: $("m1-value"),
      m2: $("m2-value"),
      m3: $("m3-value"),
    };
    for (const axis of ["m1", "m2", "m3"]) {
      values[axis].textContent = format(status.positions[axis], status.axes[axis]);
    }

    const leftReady =
      status.connected &&
      !status.estopped &&
      status.homed.m1 &&
      status.homed.m3;
    const rightReady = status.connected && !status.estopped && status.homed.m2;
    const left = document.querySelector('[data-stick="left"]');
    const right = document.querySelector('[data-stick="right"]');
    left.classList.toggle("is-disabled", !leftReady);
    left.classList.toggle("is-homed", leftReady);
    right.classList.toggle("is-disabled", !rightReady);
    right.classList.toggle("is-homed", rightReady);
    left.closest(".stick-slot").dataset.homed = String(leftReady);
    right.closest(".stick-slot").dataset.homed = String(rightReady);

    $("home").disabled = !status.connected || status.estopped;
    $("connect").classList.toggle("is-offline", !status.connected);

    if (status.error && status.error !== state.lastError) {
      showToast(status.error);
    }
    state.lastError = status.error || "";
  }

  async function refresh() {
    if (state.polling) return;
    state.polling = true;
    try {
      const payload = await api("/api/status");
      render(payload.status);
    } catch (error) {
      showToast(error.message);
    } finally {
      state.polling = false;
      clearTimeout(state.timer);
      state.timer = setTimeout(refresh, 700);
    }
  }

  async function act(request, message = "") {
    try {
      const payload = await request();
      render(payload.status);
      showToast(message);
    } catch (error) {
      showToast(error.message);
    }
  }

  $("home").addEventListener("click", () => {
    act(() =>
      api("/api/home", {
        method: "POST",
        body: JSON.stringify({ axis: "all" }),
      })
    );
  });

  $("connect").addEventListener("click", () => {
    act(() => api("/api/connect", { method: "POST" }));
  });

  $("estop").addEventListener("click", () => {
    act(
      () => api("/api/estop", { method: "POST" }),
      "Emergency stop sent."
    );
  });

  setupStick(document.querySelector('[data-stick="left"]'), [
    ["m1", "x"],
    ["m3", "y"],
  ]);
  setupStick(document.querySelector('[data-stick="right"]'), [["m2", "y"]]);
  refresh();
})();
