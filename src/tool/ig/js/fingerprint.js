(async function () {
  const hashString = (value) => {
    let hash = 2_166_136_261;
    for (let i = 0; i < value.length; i++) {
      hash ^= value.codePointAt(i);
      hash = Math.imul(hash, 16_777_619);
    }
    return (hash >>> 0).toString(16).padStart(8, "0");
  };

  const descriptorInfo = (obj, prop) => {
    const protoDescriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(obj), prop);
    const ownDescriptor = Object.getOwnPropertyDescriptor(obj, prop);
    const descriptor = protoDescriptor || ownDescriptor;
    if (!descriptor) return null;
    const getter = descriptor.get ? Function.prototype.toString.call(descriptor.get) : null;
    return {
      owner: protoDescriptor ? "prototype" : "instance",
      configurable: descriptor.configurable,
      enumerable: descriptor.enumerable,
      getterNative: getter ? getter.includes("[native code]") : null,
      getterPreview: getter ? getter.slice(0, 80) : null,
      valueType: typeof descriptor.value,
    };
  };

  const workerFingerprint = async () => {
    try {
      const source = `
                self.onmessage = () => self.postMessage({
                    userAgent: navigator.userAgent,
                    webdriver: navigator.webdriver,
                    platform: navigator.platform,
                    language: navigator.language,
                    languages: Array.from(navigator.languages || []),
                    hardwareConcurrency: navigator.hardwareConcurrency,
                    maxTouchPoints: navigator.maxTouchPoints,
                    deviceMemory: navigator.deviceMemory || null
                });
            `;
      const url = URL.createObjectURL(new Blob([source], { type: "text/javascript" }));
      const worker = new Worker(url);
      return await new Promise((resolve) => {
        let done = false;
        let timer = null;
        const finish = (value) => {
          if (done) return;
          done = true;
          clearTimeout(timer);
          worker.terminate();
          URL.revokeObjectURL(url);
          resolve(value);
        };
        timer = setTimeout(() => finish({ error: "timeout" }), 3000);
        worker.onmessage = (event) => {
          finish(event.data);
        };
        worker.onerror = (event) => {
          finish({ error: event.message || "worker error" });
        };
        worker.postMessage(null);
      });
    } catch (error) {
      return { error: error.name + ": " + error.message };
    }
  };

  const canvasFingerprint = () => {
    try {
      const canvas = document.createElement("canvas");
      canvas.width = 280;
      canvas.height = 80;
      const ctx = canvas.getContext("2d");
      ctx.textBaseline = "top";
      ctx.fillStyle = "#f60";
      ctx.fillRect(0, 0, 280, 80);
      ctx.fillStyle = "#069";
      ctx.font = "18px -apple-system, BlinkMacSystemFont, Helvetica, Arial";
      ctx.fillText("Instagram fingerprint probe 0123", 8, 8);
      ctx.globalCompositeOperation = "multiply";
      ctx.fillStyle = "rgba(120, 40, 180, 0.65)";
      ctx.beginPath();
      ctx.arc(70, 50, 28, 0, Math.PI * 2);
      ctx.fill();
      const dataUrl = canvas.toDataURL();
      return { hash: hashString(dataUrl), dataUrlLength: dataUrl.length };
    } catch (error) {
      return { error: error.name + ": " + error.message };
    }
  };

  const audioFingerprint = async () => {
    try {
      const AudioCtx = window.OfflineAudioContext || window.webkitOfflineAudioContext;
      if (!AudioCtx) return { error: "OfflineAudioContext unavailable" };
      const ctx = new AudioCtx(1, 44_100, 44_100);
      const oscillator = ctx.createOscillator();
      const compressor = ctx.createDynamicsCompressor();
      oscillator.type = "triangle";
      oscillator.frequency.value = 1000;
      compressor.threshold.value = -50;
      compressor.knee.value = 40;
      compressor.ratio.value = 12;
      compressor.attack.value = 0;
      compressor.release.value = 0.25;
      oscillator.connect(compressor);
      compressor.connect(ctx.destination);
      oscillator.start(0);
      const buffer = await ctx.startRendering();
      const data = buffer.getChannelData(0);
      let sample = "";
      for (let i = 0; i < data.length; i += 200) sample += `${data[i].toFixed(6)},`;
      return {
        hash: hashString(sample),
        sampleRate: buffer.sampleRate,
        length: buffer.length,
      };
    } catch (error) {
      return { error: error.name + ": " + error.message };
    }
  };

  const mediaDevicesFingerprint = async () => {
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
        return { error: "enumerateDevices unavailable" };
      }
      const devices = await navigator.mediaDevices.enumerateDevices();
      return devices.map((device) => ({
        kind: device.kind,
        label: device.label,
        groupId: device.groupId ? "present" : "",
        deviceId: device.deviceId ? "present" : "",
      }));
    } catch (error) {
      return { error: error.name + ": " + error.message };
    }
  };

  const fontProbe = () => {
    const fonts = [
      "-apple-system",
      "BlinkMacSystemFont",
      "Helvetica Neue",
      "Arial",
      "Segoe UI",
      "Ubuntu",
      "Noto Sans",
    ];
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    const base = {};
    const sample = "mmmmmmmmmmlli";
    for (const family of ["monospace", "serif", "sans-serif"]) {
      ctx.font = `32px ${family}`;
      base[family] = ctx.measureText(sample).width;
    }
    const measured = {};
    for (const font of fonts) {
      ctx.font = `32px "${font}", monospace`;
      measured[font] = Number(ctx.measureText(sample).width.toFixed(3));
    }
    return { base, measured };
  };

  const requestHeaders = async () => {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 4000);
      const response = await fetch("https://httpbin.org/headers", {
        cache: "no-store",
        signal: controller.signal,
      });
      clearTimeout(timer);
      if (!response.ok) return { error: `HTTP ${response.status}` };
      return await response.json();
    } catch (error) {
      return { error: error.name + ": " + error.message };
    }
  };

  const automationGlobals = () => {
    const names = [
      "__webdriver_evaluate",
      "__selenium_evaluate",
      "__webdriver_script_function",
      "__webdriver_script_func",
      "__webdriver_script_fn",
      "__fxdriver_evaluate",
      "__driver_unwrapped",
      "__webdriver_unwrapped",
      "__driver_evaluate",
      "__selenium_unwrapped",
      "_Selenium_IDE_Recorder",
      "_selenium",
      "calledSelenium",
      "_WEBDRIVER_ELEM_CACHE",
      "ChromeDriverw",
      "domAutomation",
      "domAutomationController",
    ];
    return names.filter((name) => {
      try {
        return name in window;
      } catch {
        return false;
      }
    });
  };

  let notificationPermission = null;
  try {
    notificationPermission = await navigator.permissions
      .query({ name: "notifications" })
      .then((p) => p.state);
  } catch (error) {
    notificationPermission = "error:" + error.name;
  }
  let webgl = null;
  try {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
    if (gl) {
      const dbg = gl.getExtension("WEBGL_debug_renderer_info");
      webgl = dbg
        ? {
            vendor: gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL),
            renderer: gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL),
          }
        : {
            vendor: gl.getParameter(gl.VENDOR),
            renderer: gl.getParameter(gl.RENDERER),
          };
    }
  } catch (error) {
    webgl = { error: error.name };
  }
  const descriptors = {};
  for (const prop of [
    "webdriver",
    "language",
    "languages",
    "platform",
    "hardwareConcurrency",
    "deviceMemory",
    "maxTouchPoints",
    "plugins",
    "mimeTypes",
  ]) {
    descriptors[prop] = descriptorInfo(navigator, prop);
  }

  return {
    userAgent: navigator.userAgent,
    webdriver: navigator.webdriver,
    platform: navigator.platform,
    language: navigator.language,
    languages: Array.from(navigator.languages || []),
    plugins: Array.from(navigator.plugins || []).map((p) => p.name),
    mimeTypes: Array.from(navigator.mimeTypes || []).map((m) => m.type),
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory || null,
    maxTouchPoints: navigator.maxTouchPoints,
    notificationPermission,
    hidden: document.hidden,
    visibilityState: document.visibilityState,
    inner: [window.innerWidth, window.innerHeight],
    outer: [window.outerWidth, window.outerHeight],
    screen: [
      screen.width,
      screen.height,
      screen.availWidth,
      screen.availHeight,
      screen.colorDepth,
      screen.pixelDepth,
    ],
    devicePixelRatio: window.devicePixelRatio,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    timezoneOffset: new Date().getTimezoneOffset(),
    pluginsClass: Object.prototype.toString.call(navigator.plugins),
    pluginsInstance:
      typeof PluginArray !== "undefined" ? navigator.plugins instanceof PluginArray : null,
    mimeTypesClass: Object.prototype.toString.call(navigator.mimeTypes),
    mimeTypesInstance:
      typeof MimeTypeArray !== "undefined" ? navigator.mimeTypes instanceof MimeTypeArray : null,
    descriptors,
    worker: await workerFingerprint(),
    canvas: canvasFingerprint(),
    audio: await audioFingerprint(),
    mediaDevices: await mediaDevicesFingerprint(),
    fonts: fontProbe(),
    headers: await requestHeaders(),
    automationGlobals: automationGlobals(),
    webgl,
  };
})();
