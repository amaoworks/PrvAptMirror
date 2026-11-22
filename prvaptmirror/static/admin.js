"use strict";

const account = document.querySelector(".account");
if (account) {
  document.addEventListener("click", (event) => {
    if (!account.contains(event.target)) account.open = false;
  });
  account.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && account.open) {
      account.open = false;
      account.querySelector("summary").focus();
    }
  });
}

const dialog = document.getElementById("confirm-dialog");
let pendingConfirmation;
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!form.matches("form[data-confirm]")) return;

  const count = form.dataset.confirmCount
    ? new FormData(form).getAll(form.dataset.confirmCount).length : 0;
  const message = form.dataset.confirm.replace("{count}", count);
  if (typeof dialog.showModal !== "function") {
    if (!window.confirm(message)) event.preventDefault();
    return;
  }

  event.preventDefault();
  document.getElementById("confirm-message").textContent = message;
  pendingConfirmation = { form, submitter: event.submitter };
  dialog.showModal();
});
document.getElementById("confirm-cancel").addEventListener("click", () => dialog.close());
dialog.addEventListener("close", () => { pendingConfirmation = undefined; });
document.getElementById("confirm-accept").addEventListener("click", () => {
  if (!pendingConfirmation) return;
  const { form, submitter } = pendingConfirmation;
  dialog.close();
  if (!form.isConnected) return;

  const message = form.dataset.confirm;
  delete form.dataset.confirm;
  try {
    form.requestSubmit(submitter || undefined);
  } finally {
    form.dataset.confirm = message;
  }
});

const packageForm = document.getElementById("package-bulk-delete");
if (packageForm) {
  const all = document.getElementById("select-all-packages");
  const items = Array.from(packageForm.querySelectorAll("[name='package_ids']"));
  const deleteSelected = document.getElementById("delete-selected");
  const selectedCount = document.getElementById("selected-count");
  const updateSelection = () => {
    const count = items.filter((item) => item.checked).length;
    deleteSelected.disabled = count === 0;
    selectedCount.textContent = `已选择 ${count} 个`;
    all.checked = count === items.length;
    all.indeterminate = count > 0 && count < items.length;
  };
  packageForm.addEventListener("change", (event) => {
    if (event.target === all) items.forEach((item) => { item.checked = all.checked; });
    updateSelection();
  });
  window.addEventListener("pageshow", updateSelection);
  updateSelection();
}

const sourceForm = document.querySelector("[data-source-form]");
if (sourceForm) {
  const kind = document.getElementById("kind");
  const locationField = document.getElementById("location");
  const locationLabel = sourceForm.querySelector("[data-location-label]");
  const locationHint = document.getElementById("location-hint");
  const githubFields = sourceForm.querySelector("[data-source-fields='github']");
  const matchFields = sourceForm.querySelector("[data-source-fields='match']");
  const test = sourceForm.querySelector("[data-source-test]");
  const testButton = test.querySelector("button");
  const result = document.getElementById("source-test-result");

  const updateFields = () => {
    const isGitHub = kind.value === "github_release";
    const canPreview = isGitHub || locationField.value.trim().endsWith("/");
    locationLabel.textContent = isGitHub ? "GitHub 仓库" : "下载地址";
    locationHint.textContent = isGitHub
      ? "可填写 owner/repository 或 GitHub 仓库链接。"
      : "填写 .deb 文件的完整地址，或提供以 / 结尾的 HTTP 目录地址。";
    locationField.placeholder = isGitHub ? "owner/repository" : "https://example.com/pool/";
    githubFields.hidden = githubFields.disabled = !isGitHub;
    matchFields.hidden = matchFields.disabled = !canPreview;
    test.hidden = !canPreview;
  };
  const onSourceChange = () => {
    testButton.dispatchEvent(new Event("htmx:abort"));
    result.replaceChildren();
    updateFields();
  };
  kind.addEventListener("change", onSourceChange);
  locationField.addEventListener("input", onSourceChange);
  updateFields();

  const updateAssetSelection = () => {
    const all = result.querySelector("[data-select-all-assets]");
    if (!all) return;
    const items = Array.from(result.querySelectorAll("[data-package-pattern]"));
    const count = items.filter((item) => item.checked).length;
    all.checked = items.length > 0 && count === items.length;
    all.indeterminate = count > 0 && count < items.length;
  };
  result.addEventListener("change", (event) => {
    if (event.target.matches("[data-select-all-assets]")) {
      result.querySelectorAll("[data-package-pattern]").forEach((item) => {
        item.checked = event.target.checked;
      });
    }
    updateAssetSelection();
  });
  result.addEventListener("htmx:afterSwap", updateAssetSelection);
  result.addEventListener("click", (event) => {
    if (!event.target.closest("[data-apply-packages]")) return;
    const selected = Array.from(result.querySelectorAll("[data-package-pattern]:checked"));
    const patterns = new Set(selected.map((item) => item.dataset.packagePattern));
    const pattern = selected.length ? "(?:" + Array.from(patterns).join("|") + ")" : "(?!)";
    const feedback = result.querySelector("[data-package-feedback]");
    if (pattern.length > 500) {
      feedback.textContent = "生成的正则超过 500 个字符，请减少选择或手动简化正则。";
      return;
    }
    document.getElementById("asset_pattern").value = pattern;
    feedback.textContent = `已写入正则，选择 ${selected.length} 个软件包。保存来源后生效。`;
  });

  const showRequestError = (event) => {
    const target = event.detail.target;
    if (target === result) {
      const notice = document.createElement("p");
      notice.className = "err flash";
      notice.setAttribute("role", "alert");
      notice.textContent = "预览请求失败，请稍后重试。";
      result.replaceChildren(notice);
    } else if (target?.id === "source-runtime") {
      const feedback = target.querySelector("[data-sync-feedback]");
      if (feedback) feedback.hidden = false;
    }
  };
  ["htmx:responseError", "htmx:sendError", "htmx:timeout"].forEach((name) => {
    document.body.addEventListener(name, showRequestError);
  });
}

const uploadForm = document.querySelector("[data-upload-form]");
if (uploadForm) {
  const submitButton = uploadForm.querySelector('button[type="submit"]');
  const defaultLabel = submitButton?.textContent || "";
  uploadForm.addEventListener("submit", () => {
    if (!submitButton || submitButton.disabled) return;
    submitButton.disabled = true;
    submitButton.textContent = "上传中…";
  });
  window.addEventListener("pageshow", () => {
    if (!submitButton) return;
    submitButton.disabled = false;
    submitButton.textContent = defaultLabel;
  });
}

document.querySelectorAll("[data-copy-command]").forEach((button) => {
  button.addEventListener("click", async () => {
    const box = button.closest(".code-box");
    const command = box.querySelector("pre");
    const feedback = box.nextElementSibling;
    try {
      await navigator.clipboard.writeText(command.textContent);
      feedback.textContent = "命令已复制。";
    } catch {
      const range = document.createRange();
      range.selectNodeContents(command);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      let copied = false;
      try {
        copied = document.execCommand("copy");
      } catch {
        // Leave the command selected for manual copying.
      }
      if (copied) selection.removeAllRanges();
      feedback.textContent = copied ? "命令已复制。" : "命令已选中，请手动复制。";
    }
  });
});
