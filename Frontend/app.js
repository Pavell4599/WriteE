const screens = [
  document.querySelector("#screen-input"),
  document.querySelector("#screen-svg"),
  document.querySelector("#screen-plotter"),
];
const mainMenu = document.querySelector("#main-menu");
const projectForm = document.querySelector("#project-form");
const progress = document.querySelector("#progress");
const progressBars = [...document.querySelectorAll(".progress span")];
const stepCount = document.querySelector("#step-count");
const sourceFile = document.querySelector("#source-file");
const traceMode = document.querySelector("#trace-mode");
const prompt = document.querySelector("#prompt");
const promptFiles = document.querySelector("#prompt-files");
const fileRow = document.querySelector("#file-row");
const attachmentList = document.querySelector("#attachment-list");
const inputError = document.querySelector("#input-error");
const svgError = document.querySelector("#svg-error");
const plotterError = document.querySelector("#plotter-error");
const dropzone = document.querySelector("#dropzone");
const prepareButton = document.querySelector("#prepare-svg");
const createGcodeButton = document.querySelector("#create-gcode");
const checkFluidNcButton = document.querySelector("#check-fluidnc");
const sendGcodeButton = document.querySelector("#send-gcode");
const hostInput = document.querySelector("#fluidnc-host");
const penLiftMode = document.querySelector("#pen-lift-mode");
const servoSettings = document.querySelector("#servo-settings");
const zSettings = document.querySelector("#z-settings");
const servoUpInput = document.querySelector("#servo-up");
const servoDownInput = document.querySelector("#servo-down");
const servoDelayInput = document.querySelector("#servo-delay");
const servoCalibrated = document.querySelector("#servo-calibrated");
const traceThreshold = document.querySelector("#trace-threshold");
let currentStep = 0;
let attachments = [];
let currentSvg = "";
let currentGcode = "";
let currentJobId = "";
let previewUrl = "";
let fluidNcReady = false;
let preflightTimer = null;

function showMenu() {
  mainMenu.hidden = false;
  projectForm.hidden = true;
  progress.hidden = true;
  stepCount.hidden = true;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showStep(step) {
  currentStep = step;
  mainMenu.hidden = true;
  projectForm.hidden = false;
  progress.hidden = false;
  stepCount.hidden = false;
  screens.forEach((screen, index) => {
    const active = index === step;
    screen.hidden = !active;
    screen.classList.toggle("active", active);
  });
  progressBars.forEach((bar, index) => {
    bar.classList.toggle("current", index <= step);
  });
  stepCount.textContent = `ШАГ ${step + 1} / ${screens.length}`;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showError(element, message) {
  element.textContent = message;
  element.classList.toggle("hidden", !message);
}

function canSendGcode() {
  return Boolean(currentJobId && fluidNcReady && (penLiftMode.value !== "servo" || servoCalibrated.checked));
}

function refreshSendButton() {
  sendGcodeButton.disabled = !canSendGcode();
}

async function readJson(response) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.toLowerCase().includes("application/json")) {
    const message = response.ok
      ? "СЕРВЕР ВЕРНУЛ НЕ JSON. ПЕРЕЗАГРУЗИТЕ СТРАНИЦУ."
      : `ОШИБКА СЕРВЕРА ${response.status}. ПРОВЕРЬТЕ ЖУРНАЛ BACKEND.`;
    throw new Error(message);
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("СЕРВЕР ВЕРНУЛ НЕКОРРЕКТНЫЙ JSON.");
  }
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function setFile(file) {
  if (!file) return;
  const isImage = file.type.startsWith("image/") || /\.svg$/i.test(file.name);
  if (!isImage || file.size > 20 * 1024 * 1024) {
    sourceFile.value = "";
    fileRow.classList.add("hidden");
    showError(inputError, isImage ? "ФАЙЛ БОЛЬШЕ 20 МБ." : "ВЫБЕРИТЕ ФАЙЛ ИЗОБРАЖЕНИЯ.");
    return;
  }
  fileRow.querySelector("#file-name").textContent = file.name;
  fileRow.classList.remove("hidden");
  showError(inputError, "");
}

function updateAttachments(files) {
  attachments = files;
  const transfer = new DataTransfer();
  attachments.forEach((file) => transfer.items.add(file));
  promptFiles.files = transfer.files;
  attachmentList.replaceChildren();
  attachmentList.hidden = attachments.length === 0;
  document.querySelector("#attachment-count").textContent = `ФАЙЛОВ: ${attachments.length}`;

  attachments.forEach((file, index) => {
    const item = document.createElement("li");
    item.className = "attachment-item";
    const name = document.createElement("span");
    name.textContent = file.name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `УДАЛИТЬ ${file.name}`);
    remove.addEventListener("click", () => {
      updateAttachments(attachments.filter((_, fileIndex) => fileIndex !== index));
    });
    item.append(name, remove);
    attachmentList.append(item);
  });
}

async function postImageForSvg(file) {
  const formData = new FormData();
  formData.append("file", file, file.name);
  formData.append("mode", traceMode.value);
  const threshold = thresholdValue();
  if (threshold !== null) formData.append("threshold", String(threshold));
  const response = await fetch("/api/vectorize", { method: "POST", body: formData });
  return readJson(response);
}

async function generateImageFromPrompt() {
  const response = await fetch("/api/generate-image", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt: prompt.value.trim(),
      allow_external: document.querySelector("#external-ai-consent").checked,
    }),
  });
  if (!response.ok) {
    await readJson(response);
    throw new Error(`HTTP ${response.status}`);
  }
  return new File([await response.blob()], "generated.png", { type: "image/png" });
}

async function prepareSvg() {
  showError(inputError, "");
  const source = sourceFile.files[0]
    || attachments.find((file) => file.type.startsWith("image/") || /\.svg$/i.test(file.name));
  if (!source && !prompt.value.trim()) {
    showError(inputError, "ДОБАВЬТЕ ИЗОБРАЖЕНИЕ ИЛИ ТЕКСТ.");
    return;
  }

  prepareButton.disabled = true;
  prepareButton.textContent = "ОБРАБОТКА…";
  try {
    const file = source || await generateImageFromPrompt();
    const result = await postImageForSvg(file);
    currentSvg = result.svg;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(new Blob([currentSvg], { type: "image/svg+xml" }));
    document.querySelector("#svg-preview").src = previewUrl;
    document.querySelector("#svg-preview").hidden = false;
    document.querySelector("#svg-empty").hidden = true;
    showStep(1);
    schedulePreflight();
  } catch (error) {
    showError(inputError, error.message);
  } finally {
    prepareButton.disabled = false;
    prepareButton.innerHTML = 'СОЗДАТЬ SVG <span aria-hidden="true">→</span>';
  }
}

async function createGcode() {
  showError(svgError, "");
  currentJobId = "";
  refreshSendButton();
  createGcodeButton.disabled = true;
  createGcodeButton.textContent = "ОБРАБОТКА…";
  const value = (name) => Number(projectForm.elements.namedItem(name).value);
  try {
    const response = await fetch("/api/convert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        svg: currentSvg,
        bed_width_mm: value("bed_width_mm"),
        bed_height_mm: value("bed_height_mm"),
        speed_mm_s: value("speed_mm_s"),
        pen_mode: penLiftMode.value,
        z_up_mm: value("z_up_mm"),
        z_down_mm: value("z_down_mm"),
        servo_up_s: Number(servoUpInput.value),
        servo_down_s: Number(servoDownInput.value),
        servo_delay_ms: Number(servoDelayInput.value),
        offset_x_percent: value("offset_x_percent"),
        offset_y_percent: value("offset_y_percent"),
      }),
    });
    const result = await readJson(response);
    currentGcode = result.gcode;
    currentJobId = result.job_id;
    document.querySelector("#gcode").value = currentGcode;
    document.querySelector("#download-gcode").disabled = false;
    refreshSendButton();
    showStep(2);
  } catch (error) {
    showError(svgError, error.message);
  } finally {
    createGcodeButton.disabled = false;
    createGcodeButton.innerHTML = 'СОЗДАТЬ G-CODE <span aria-hidden="true">→</span>';
  }
}

async function checkFluidNc() {
  showError(plotterError, "");
  fluidNcReady = false;
  sendGcodeButton.disabled = true;
  checkFluidNcButton.disabled = true;
  document.querySelector("#connection-message").textContent = "ПРОВЕРКА СОЕДИНЕНИЯ…";
  try {
    const response = await fetch("/api/fluidnc/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host: hostInput.value.trim() }),
    });
    const result = await readJson(response);
    fluidNcReady = result.state === "Idle";
    document.querySelector(".device-state strong").textContent = `FLUIDNC: ${result.state.toUpperCase()}`;
    document.querySelector(".status-dot").classList.toggle("ready", fluidNcReady);
    document.querySelector("#connection-message").textContent = result.report;
    refreshSendButton();
    localStorage.setItem("fluidnc-host", hostInput.value.trim());
  } catch (error) {
    document.querySelector(".device-state strong").textContent = "НЕ ПОДКЛЮЧЕН";
    document.querySelector(".status-dot").classList.remove("ready");
    document.querySelector("#connection-message").textContent = "НЕТ СВЯЗИ С FLUIDNC.";
    showError(plotterError, error.message);
  } finally {
    checkFluidNcButton.disabled = false;
  }
}

async function sendGcode() {
  showError(plotterError, "");
  if (!canSendGcode()) return;
  const confirmed = window.confirm("ВЫПОЛНЕН HOME? ПРОВЕРЬТЕ НАПРАВЛЕНИЕ СЕРВО И РАБОЧЕЕ ПОЛЕ. ОТПРАВИТЬ G-CODE?");
  if (!confirmed) return;
  sendGcodeButton.disabled = true;
  sendGcodeButton.textContent = "ОТПРАВКА…";
  try {
    const response = await fetch("/api/fluidnc/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        host: hostInput.value.trim(),
        job_id: currentJobId,
        confirm: true,
        servo_calibrated: servoCalibrated.checked,
      }),
    });
    const result = await readJson(response);
    document.querySelector("#connection-message").textContent = `ОТПРАВЛЕНО КОМАНД: ${result.command_count}`;
    document.querySelector(".device-state strong").textContent = "ЗАДАНИЕ ОТПРАВЛЕНО";
    document.querySelector(".status-dot").classList.remove("ready");
    fluidNcReady = false;
  } catch (error) {
    showError(plotterError, error.message);
    document.querySelector(".device-state strong").textContent = "ПРОВЕРЬТЕ СОСТОЯНИЕ";
    document.querySelector(".status-dot").classList.remove("ready");
    document.querySelector("#connection-message").textContent = "ПЕРЕД ПОВТОРОМ ПРОВЕРЬТЕ ПЛОТТЕР.";
    fluidNcReady = false;
  } finally {
    sendGcodeButton.disabled = true;
    sendGcodeButton.textContent = "РИСОВАТЬ";
  }
}

function thresholdValue() {
  if (traceThreshold.value === "0") return null;
  return Number(traceThreshold.value);
}

async function runPreflight() {
  if (!currentSvg) return;
  const preflight = document.querySelector("#preflight");
  preflight.classList.add("loading");
  preflight.innerHTML = '<span class="preflight-status">РАСЧЁТ…</span>';
  const value = (name) => Number(projectForm.elements.namedItem(name).value);
  try {
    const response = await fetch("/api/preflight", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        svg: currentSvg,
        bed_width_mm: value("bed_width_mm"), bed_height_mm: value("bed_height_mm"),
        speed_mm_s: value("speed_mm_s"), pen_mode: penLiftMode.value,
        z_up_mm: value("z_up_mm"), z_down_mm: value("z_down_mm"),
        servo_up_s: Number(servoUpInput.value), servo_down_s: Number(servoDownInput.value),
        servo_delay_ms: Number(servoDelayInput.value),
        offset_x_percent: value("offset_x_percent"), offset_y_percent: value("offset_y_percent"),
      }),
    });
    const result = await readJson(response);
    const metrics = result;
    const time = metrics.estimated_seconds < 60
      ? `${metrics.estimated_seconds} СЕК`
      : `${Math.round(metrics.estimated_seconds / 60)} МИН`;
    preflight.classList.toggle("has-warning", Boolean(metrics.warnings?.length));
    preflight.innerHTML = `
      <div><span>ДЛИНА ЛИНИИ</span><strong>${metrics.drawing_length_mm} ММ</strong></div>
      <div><span>ХОЛОСТЫЕ ХОДЫ</span><strong>${metrics.travel_length_mm} ММ</strong></div>
      <div><span>ВРЕМЯ ПРИБЛ.</span><strong>${time}</strong></div>
      <div><span>КОМАНД</span><strong>${metrics.line_count}</strong></div>
      ${metrics.warnings?.map((warning) => `<p class="preflight-warning">${warning}</p>`).join("") || '<p class="preflight-ready">ПРЕДПОЛЁТНАЯ ПРОВЕРКА ПРОЙДЕНА</p>'}`;
  } catch (error) {
    preflight.classList.add("has-warning");
    preflight.innerHTML = `<p class="preflight-warning">${error.message}</p>`;
  } finally {
    preflight.classList.remove("loading");
  }
}

function schedulePreflight() {
  clearTimeout(preflightTimer);
  preflightTimer = setTimeout(runPreflight, 180);
}

document.querySelector("[data-start]").addEventListener("click", () => showStep(0));
document.querySelector("[data-menu]").addEventListener("click", showMenu);
document.querySelectorAll("[data-back]").forEach((button) => {
  button.addEventListener("click", () => showStep(Math.max(currentStep - 1, 0)));
});
prepareButton.addEventListener("click", prepareSvg);
createGcodeButton.addEventListener("click", createGcode);
checkFluidNcButton.addEventListener("click", checkFluidNc);
sendGcodeButton.addEventListener("click", sendGcode);
hostInput.addEventListener("input", () => {
  fluidNcReady = false;
  sendGcodeButton.disabled = true;
  document.querySelector(".device-state strong").textContent = "НЕ ПОДКЛЮЧЕН";
  document.querySelector(".status-dot").classList.remove("ready");
});
penLiftMode.addEventListener("change", () => {
  servoSettings.hidden = penLiftMode.value !== "servo";
  zSettings.hidden = penLiftMode.value === "servo";
  servoCalibrated.checked = false;
  currentJobId = "";
  refreshSendButton();
  schedulePreflight();
});
document.querySelectorAll("#screen-svg .settings input:not(#servo-calibrated), #screen-svg .settings select").forEach((input) => {
  input.addEventListener("input", () => {
    currentJobId = "";
    if ([servoUpInput, servoDownInput, servoDelayInput].includes(input)) {
      servoCalibrated.checked = false;
    }
    refreshSendButton();
    schedulePreflight();
  });
});
servoCalibrated.addEventListener("change", refreshSendButton);

sourceFile.addEventListener("change", () => setFile(sourceFile.files[0]));
document.querySelector("#remove-file").addEventListener("click", () => {
  sourceFile.value = "";
  fileRow.classList.add("hidden");
});
prompt.addEventListener("input", () => showError(inputError, ""));
document.querySelector("#attach-files").addEventListener("click", () => promptFiles.click());
promptFiles.addEventListener("change", () => {
  const selected = [...promptFiles.files];
  const valid = selected.filter((file) => file.size <= 20 * 1024 * 1024);
  if (valid.length !== selected.length) showError(inputError, "ФАЙЛ БОЛЬШЕ 20 МБ.");
  const unique = new Map(
    [...attachments, ...valid].map((file) => [`${file.name}:${file.size}:${file.lastModified}`, file]),
  );
  updateAttachments([...unique.values()]);
  if (valid.length === selected.length) showError(inputError, "");
});

["dragenter", "dragover"].forEach((eventName) => {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.add("drag-over");
  });
});
["dragleave", "drop"].forEach((eventName) => {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.remove("drag-over");
  });
});
dropzone.addEventListener("drop", (event) => {
  const file = event.dataTransfer.files[0];
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  sourceFile.files = transfer.files;
  setFile(file);
});

document.querySelector("#download-gcode").addEventListener("click", () => {
  if (!currentGcode) return;
  const url = URL.createObjectURL(new Blob([currentGcode], { type: "text/plain" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "drawing.gcode";
  link.click();
  URL.revokeObjectURL(url);
});

hostInput.value = localStorage.getItem("fluidnc-host") || "";
window.addEventListener("beforeunload", () => {
  if (currentGcode) {
    const blob = new Blob([currentGcode], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "drawing.gcode";
    link.click();
    URL.revokeObjectURL(url);
  }
});

traceThreshold.addEventListener("input", () => {
  document.querySelector("#threshold-value").textContent = traceThreshold.value === "0" ? "АВТО" : Number(traceThreshold.value).toFixed(2);
});

traceMode.addEventListener("change", schedulePreflight);
