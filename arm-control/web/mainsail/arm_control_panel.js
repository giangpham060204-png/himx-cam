// arm_control_panel.js — Arm control panel for Mainsail (3-DOF camera arm).
//
// Talks ONLY to arm_web.py's /api/* endpoints so every motion keeps passing
// through the ArmController safety layer (e-stop latch, homed trust, M2
// cable-wind guard, audit trail). Never addresses Moonraker directly.
//
// Host adapter: registered as a Vue options component on
// MOONRAKER.customPanels, following the KlipperEndstopPanel template
// pattern. CONFIRM against the installed Mainsail version with the Task 5
// spike (arm-control/documentations/mainsail-panel.md) before deploy; the
// adapter is the registration block at the bottom of this file and is the
// only thing the spike should refine.

window.MOONRAKER = window.MOONRAKER || {};
window.MOONRAKER.customPanels = window.MOONRAKER.customPanels || {};

window.MOONRAKER.customPanels.armControlPanel = {
    name: "Arm control",
    icon: "mdi-ray-vertex",
    sidebar_icon: true,

    data() {
        return {
            axes: [
                { id: "m1", label: "M1", vertical: true },
                { id: "m2", label: "M2", vertical: false },
                { id: "m3", label: "M3", vertical: false },
            ],
            status: null,
            baseUrl: localStorage.getItem("arm.panel.base")
                || "http://" + location.hostname + ":8765",
            token: localStorage.getItem("arm.panel.token") || "",
            busy: false,
            timer: null,
            toastTimer: null,
            reconnectAt: 0,
            toast: "",
            settingsVisible: false,
            draftBaseUrl: "",
            draftToken: "",
        };
    },

    mounted() {
        this.ensureStyles();
        this.startPolling();
    },

    beforeDestroy() {
        clearTimeout(this.timer);
        clearTimeout(this.toastTimer);
    },

    computed: {
        alarm() {
            const s = this.status;
            return Boolean(
                s && (s.estopped || s.faulted || s.klippy_state === "shutdown")
            );
        },
        ledClass() {
            const s = this.status;
            if (!s) return "";
            if (this.alarm) return "is-alarm";
            return s.connected && s.klippy_state === "ready" ? "is-ready" : "";
        },
        ledLabel() {
            const s = this.status;
            if (!s) return "Offline";
            if (this.alarm) return "E-stop latched";
            return s.connected && s.klippy_state === "ready"
                ? "Connected" : "Offline";
        },
        safetyLine() {
            const s = this.status;
            if (!s) return "";
            const parts = [];
            parts.push(s.connected ? "moonraker up" : "moonraker offline");
            if (s.faulted) parts.push("fault: " + (s.fault_reason || "?"));
            if (s.stop_requested) parts.push("controlled stop");
            if (s.error) parts.push(s.error);
            return parts.join(" · ");
        },
    },

    methods: {
        // Tokens kept in sync with arm-control/web/style.css (plan Task 9
        // updates both). Mono is digits-only; copper is interaction-only;
        // teal/alarm are state colors only.
        ensureStyles() {
            if (document.getElementById("arm-panel-style")) return;
            const style = document.createElement("style");
            style.id = "arm-panel-style";
            style.textContent = ([
                ".arm-panel{background:#171916;color:#e7e1d3;font-family:system-ui,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;border-radius:10px;border:1px solid #20211d}",
                ".arm-panel .v-card__title.arm-head{color:#e7e1d3;display:flex;gap:8px;align-items:center}",
                ".arm-title{font-size:13px;letter-spacing:.12em;color:#777366}",
                /* deslop-ignore-next-line 19 LED dot is a real indicator lamp */
                ".arm-led{width:11px;height:11px;border-radius:50%;background:#777366;flex:0 0 auto}",
                ".arm-led.is-ready{background:#3f8d82;box-shadow:0 0 0 4px rgba(63,141,130,.16)}",
                ".arm-led.is-alarm{background:#b3261e;box-shadow:0 0 0 4px rgba(179,38,30,.18);animation:arm-led-pulse 900ms ease-in-out infinite alternate}",
                ".arm-safety{font-size:10px;letter-spacing:.1em;color:#777366;min-height:14px;margin:4px 0 10px}",
                ".arm-stage{position:relative;padding:14px 8px 6px;background:#20211d;border-radius:10px;box-shadow:0 1px 0 rgba(255,255,255,.06) inset,0 10px 22px rgba(0,0,0,.4);user-select:none}",
                ".arm-pivot{display:block;width:96px;height:96px;color:rgba(119,115,102,.25);position:absolute;top:36px;left:50%;transform:translateX(-50%);z-index:0;pointer-events:none}",
                /* deslop-ignore-next-line 19 e-stop mushroom is a physical metaphor */
                ".arm-estop{position:relative;z-index:1;display:grid;place-items:center;width:74px;height:74px;border-radius:50%;background:#b3261e;margin:14px auto 10px;box-shadow:0 0 0 4px #171916,0 6px 0 #7f1712}",
                ".arm-estop .arm-stop-mark{width:20px;height:20px;border:2px solid #fff7f2}",
                ".arm-axes{display:flex;justify-content:center;gap:34px;padding:10px 0 2px;position:relative;z-index:1}",
                ".arm-axis{display:flex;flex-direction:column;align-items:center;gap:4px}",
                ".arm-label{font-size:10px;letter-spacing:.12em;color:#777366}",
                /* deslop-ignore-next-line 34 digits-only readout */
                ".arm-value{min-height:22px;color:#777366;font-family:'IBM Plex Mono','SFMono-Regular',Consolas,'Liberation Mono',monospace;font-size:19px;font-variant-numeric:tabular-nums;letter-spacing:-0.04em}",
                ".arm-axis[data-homed='true'] .arm-value{color:#3f8d82}",
                ".arm-rocker{display:flex;gap:4px;margin-top:2px;padding:5px;border-radius:14px;background:#171916;box-shadow:0 2px 0 rgba(255,255,255,.06) inset,0 6px 12px rgba(0,0,0,.25)}",
                ".arm-rocker-btn{width:52px !important;height:40px !important;min-width:0 !important;border-radius:12px;background:#2c2d27 !important;color:#e7e1d3 !important;box-shadow:none;transition:background 100ms ease}",
                ".arm-axis--horizontal .arm-rocker-btn{width:44px !important}",
                ".arm-rocker-btn:hover{background:#a4552f !important}",
                ".arm-actions{display:flex;justify-content:flex-start;padding:10px 4px 2px;position:relative;z-index:1}",
                ".arm-home{border:1px solid #2c2d27;border-radius:999px;background:transparent;color:#777366;font-size:10px;letter-spacing:.14em;box-shadow:none}",
                ".arm-home:hover{background:#20211d;color:#e7e1d3}",
                ".arm-estop:hover{background:#7f1712}.arm-estop:active{transform:translateY(2px);box-shadow:0 0 0 4px #171916,0 3px 0 #7f1712}",
                ".arm-settings{background:#1d1e1a !important;border:1px solid #2c2d27;margin-top:10px}",
                ".arm-toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);max-width:min(540px,calc(100vw - 32px));padding:9px 12px;border:1px solid #777366;background:#20211d;color:#e7e1d3;font-size:11px;line-height:1.35;z-index:600;box-shadow:0 6px 14px rgba(0,0,0,.35)}",
                "@keyframes arm-led-pulse{from{opacity:.45}to{opacity:1}}",
                "@media (prefers-reduced-motion:reduce){.arm-panel *{animation:none !important;transition:none !important}}",
            ]).join("\n");
            document.head.appendChild(style);
        },

        fmt(value, axis) {
            if (value === null || value === undefined || Number.isNaN(Number(value))) {
                return "—";
            }
            const digits = axis && axis.kind === "rotary" ? 1 : 2;
            return Number(Number(value).toFixed(digits)).toString();
        },

        axesMeta() {
            return (this.status && this.status.axes) || {};
        },

        showToast(message) {
            clearTimeout(this.toastTimer);
            this.toast = message || "";
            if (message) {
                this.toastTimer = setTimeout(() => { this.toast = ""; }, 4500);
            }
        },

        api(path, options) {
            options = options || {};
            return fetch(this.baseUrl + path, {
                method: options.method || "GET",
                headers: {
                    "Content-Type": "application/json",
                    "X-Arm-Token": this.token,
                },
                body: options.body === undefined
                    ? undefined : JSON.stringify(options.body),
            }).then((response) =>
                response.json().then((payload) => {
                    if (!response.ok || !payload.ok) {
                        const error = new Error(
                            payload && payload.error
                                ? payload.error : "HTTP " + response.status
                        );
                        error.status = response.status;
                        throw error;
                    }
                    return payload;
                })
            );
        },

        startPolling() {
            if (this.busy) {
                this.schedule();
                return;
            }
            this.busy = true;
            this.api("/api/status")
                .then((payload) => {
                    this.status = payload.status;
                    if (!payload.status.connected && Date.now() > this.reconnectAt) {
                        this.reconnectAt = Date.now() + 2500;
                        this.api("/api/connect", { method: "POST" })
                            .then((p) => { this.status = p.status; })
                            .catch(() => {});
                    }
                })
                .catch((error) => this.surface(error))
                .then(() => {
                    this.busy = false;
                    this.schedule();
                });
        },

        schedule() {
            clearTimeout(this.timer);
            this.timer = setTimeout(this.startPolling, 700);
        },

        surface(error) {
            this.showToast(
                error.status === 403
                    ? "token rejected — paste a fresh token from arm-web"
                    : error.message
            );
            if (error.status === 403) this.openSettings();
        },

        send(path, body) {
            this.api(path, { method: "POST", body: body || {} })
                .then((payload) => { this.status = payload.status; })
                .catch((error) => this.surface(error));
        },

        jog(axis, delta) {
            this.send("/api/jog", { axis: axis, delta: delta });
        },

        homeAll() {
            this.send("/api/home", { axis: "all" });
        },

        estop() {
            // Never disabled, never confirmed: the latch lives in
            // ArmController and the physical e-stop stays primary.
            this.send("/api/estop");
        },

        openSettings() {
            this.draftBaseUrl = this.baseUrl;
            this.draftToken = "";
            this.settingsVisible = true;
        },

        saveSettings() {
            const base = (this.draftBaseUrl || "").trim();
            const nextToken = (this.draftToken || "").trim();
            if (base) this.baseUrl = base;
            if (nextToken) this.token = nextToken;
            localStorage.setItem("arm.panel.base", this.baseUrl);
            localStorage.setItem("arm.panel.token", this.token);
            this.settingsVisible = false;
            this.showToast("saved");
        },
    },

    template: `
        <v-card class="arm-panel"
                :data-estopped="String(Boolean(status && status.estopped))"
                :class="{ 'arm-panel--alarm': alarm }">
            <v-card-title class="arm-head py-2">
                <span class="arm-led" :class="ledClass" role="status" :aria-label="ledLabel"></span>
                <span class="arm-title">Arm control</span>
                <v-spacer></v-spacer>
                <v-btn small icon aria-label="Panel settings" @click="openSettings">
                    <v-icon small>mdi-cog</v-icon>
                </v-btn>
            </v-card-title>
            <v-divider></v-divider>
            <v-card-text>
                <div class="arm-safety">{{ safetyLine }}</div>

                <div class="arm-stage">
                    <svg class="arm-pivot" viewBox="0 0 100 100" aria-hidden="true">
                        <path d="M50 8v20M50 72v20M8 50h20M72 50h20"
                              stroke="currentColor" stroke-width="1.5" fill="none"/>
                    </svg>

                    <v-btn class="arm-estop" width="74" height="74"
                           aria-label="Emergency stop" @click="estop">
                        <span class="arm-stop-mark"></span>
                    </v-btn>

                    <div class="arm-axes">
                        <div v-for="axis in axes" :key="axis.id"
                             class="arm-axis"
                             :class="{ 'arm-axis--horizontal': !axis.vertical }"
                             :data-homed="String(Boolean(status && status.homed && status.homed[axis.id]))">
                            <span class="arm-label">{{ axis.label }}</span>
                            <span class="arm-value">{{ fmt(status && status.positions && status.positions[axis.id], axesMeta()[axis.id]) }}</span>
                            <div class="arm-rocker">
                                <template v-if="axis.vertical">
                                    <v-btn small class="arm-rocker-btn"
                                           :aria-label="axis.label + ' up'"
                                           @click="jog(axis.id, 1)">
                                        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M6 14l6-6 6 6" fill="none" stroke="currentColor" stroke-width="2"/></svg>
                                    </v-btn>
                                    <v-btn small class="arm-rocker-btn"
                                           :aria-label="axis.label + ' down'"
                                           @click="jog(axis.id, -1)">
                                        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M6 10l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2"/></svg>
                                    </v-btn>
                                </template>
                                <template v-else>
                                    <v-btn small class="arm-rocker-btn"
                                           :aria-label="axis.label + ' left'"
                                           @click="jog(axis.id, -1)">
                                        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M14 6l-6 6 6 6" fill="none" stroke="currentColor" stroke-width="2"/></svg>
                                    </v-btn>
                                    <v-btn small class="arm-rocker-btn"
                                           :aria-label="axis.label + ' right'"
                                           @click="jog(axis.id, 1)">
                                        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M10 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2"/></svg>
                                    </v-btn>
                                </template>
                            </div>
                        </div>
                    </div>

                    <div class="arm-actions">
                        <v-btn small class="arm-home" aria-label="Home all axes"
                               @click="homeAll">HOME</v-btn>
                    </div>
                </div>

                <v-alert v-if="settingsVisible" dense class="arm-settings">
                    <v-text-field v-model="draftBaseUrl" label="arm-web base URL" dense hide-details></v-text-field>
                    <v-text-field v-model="draftToken"
                                  label="per-run token (from arm-web --show-token)"
                                  type="password" dense hide-details></v-text-field>
                    <v-btn small class="arm-save" @click="saveSettings">Save</v-btn>
                </v-alert>

                <div class="arm-toast" role="status" aria-live="polite" :hidden="!toast">{{ toast }}</div>
            </v-card-text>
        </v-card>
        `,
};
