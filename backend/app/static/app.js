import {
  Activity,
  ArrowUp,
  ArrowUpRight,
  BadgeCheck,
  Binary,
  Braces,
  BrainCircuit,
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  createIcons,
  Database,
  Eye,
  File,
  FileCode2,
  FileScan,
  FileText,
  FileType2,
  FileUp,
  FileWarning,
  Files,
  LibraryBig,
  ListChecks,
  ListFilter,
  LoaderCircle,
  LogIn,
  LogOut,
  Menu,
  MessagesSquare,
  Plus,
  Quote,
  RefreshCw,
  RotateCcw,
  ScanSearch,
  Search,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Square,
  UploadCloud,
  Workflow,
  X
} from "lucide";
import {
  AuthenticationRequiredError,
  OidcAuthManager
} from "./auth.js";

const state = {
  accessToken: "",
  accessTokenExpiresAt: 0,
  authConfig: null,
  oidcAuthManager: null,
  authRecoveryNoticeShown: false,
  currentUser: null,
  documents: [],
  jobs: [],
  uploadMode: "file",
  selectedFile: null,
  activeView: "research",
  querying: false,
  queryMode: "quick",
  researchJobId: null,
  lastAnswer: null,
  detailRequest: 0,
  detailCloseTimer: null,
  pdfObjectUrl: null
};

const viewNames = {
  research: "知识问答",
  documents: "文档库",
  upload: "上传索引",
  jobs: "索引任务",
  system: "系统状态"
};

const userLabels = {
  u_sales: "销售用户",
  u_hr: "HR 用户",
  u_finance: "财务用户",
  u_admin: "系统管理员"
};

const statusLabels = {
  queued: "排队中",
  running: "处理中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消"
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function refreshIcons() {
  createIcons({
    icons: {
      Activity,
      ArrowUp,
      ArrowUpRight,
      BadgeCheck,
      Binary,
      Braces,
      BrainCircuit,
      Check,
      ChevronDown,
      ChevronRight,
      CircleAlert,
      CircleCheck,
      Database,
      Eye,
      File,
      FileCode2,
      FileScan,
      FileText,
      FileType2,
      FileUp,
      FileWarning,
      Files,
      LibraryBig,
      ListChecks,
      ListFilter,
      LoaderCircle,
      LogIn,
      LogOut,
      Menu,
      MessagesSquare,
      Plus,
      Quote,
      RefreshCw,
      RotateCcw,
      ScanSearch,
      Search,
      ShieldAlert,
      ShieldCheck,
      Sparkles,
      Square,
      UploadCloud,
      Workflow,
      X
    },
    attrs: { "stroke-width": 1.8 }
  });
}

async function requestJson(url, options = {}) {
  const { body } = options;
  const isForm = body instanceof FormData;
  const response = await authenticatedFetch(url, {
    ...options,
    headers: {
      ...(!isForm && body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {})
    }
  });
  const raw = await response.text();
  let data = null;
  if (raw) {
    try { data = JSON.parse(raw); } catch { data = raw; }
  }
  if (!response.ok) {
    const rawDetail = data && typeof data === "object" ? data.detail : data;
    const detail = rawDetail && typeof rawDetail === "object"
      ? rawDetail.message || rawDetail.code
      : rawDetail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || response.statusText));
  }
  return data;
}

async function authenticatedFetch(url, options = {}) {
  const {
    headers = {},
    authenticated = true,
    retryUnauthorized = true,
    ...fetchOptions
  } = options;
  let token = state.accessToken;
  if (authenticated && state.authConfig?.mode === "oidc") {
    try {
      token = await state.oidcAuthManager.getAccessToken();
      state.accessToken = token;
      state.accessTokenExpiresAt = state.oidcAuthManager.accessTokenExpiresAt;
    } catch (error) {
      if (error instanceof AuthenticationRequiredError) handleAuthenticationRequired(error);
      throw error;
    }
  }

  const send = (accessToken) => fetch(url, {
    ...fetchOptions,
    headers: {
      ...(authenticated && accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...headers
    }
  });
  let response = await send(token);
  if (response.status !== 401 || !authenticated || !retryUnauthorized) return response;

  if (state.authConfig?.mode === "oidc" && state.oidcAuthManager) {
    try {
      token = await state.oidcAuthManager.recoverFromUnauthorized();
      state.accessToken = token;
      state.accessTokenExpiresAt = state.oidcAuthManager.accessTokenExpiresAt;
      response = await send(token);
      if (response.status !== 401) return response;
    } catch (error) {
      if (!(error instanceof AuthenticationRequiredError)) throw error;
    }
  }
  const error = new AuthenticationRequiredError();
  handleAuthenticationRequired(error);
  throw error;
}

async function requestBlob(url, options = {}) {
  const response = await authenticatedFetch(url, options);
  if (!response.ok) {
    const raw = await response.text();
    let detail = raw;
    try { detail = JSON.parse(raw).detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : response.statusText);
  }
  return response.blob();
}

function showToast(title, message = "", type = "success") {
  const toast = document.createElement("div");
  toast.className = `toast${type === "error" ? " is-error" : ""}`;
  toast.innerHTML = `
    <i data-lucide="${type === "error" ? "circle-alert" : "circle-check"}"></i>
    <div><strong>${escapeHtml(title)}</strong>${message ? `<span>${escapeHtml(message)}</span>` : ""}</div>
    <button type="button" aria-label="关闭通知"><i data-lucide="x"></i></button>
  `;
  $("#toastRegion").appendChild(toast);
  toast.querySelector("button").addEventListener("click", () => toast.remove());
  refreshIcons();
  window.setTimeout(() => toast.remove(), 5000);
}

function switchView(view) {
  if (!viewNames[view]) return;
  state.activeView = view;
  $$('[data-view-panel]').forEach((panel) => {
    const active = panel.dataset.viewPanel === view;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  });
  $$(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === view));
  $("#currentViewName").textContent = viewNames[view];
  history.replaceState(null, "", `#${view}`);
  closeMobileNav();
  if (view === "documents") loadDocuments();
  if (view === "jobs") loadJobs();
  if (view === "system") loadSystemStatus();
}

function openMobileNav() {
  $("#sidebar").classList.add("is-open");
  $("#mobileScrim").hidden = false;
}

function closeMobileNav() {
  $("#sidebar").classList.remove("is-open");
  $("#mobileScrim").hidden = true;
}

async function checkHealth() {
  const serviceState = $("#serviceState");
  try {
    await requestJson("/health", { authenticated: false });
    serviceState.className = "service-state is-ok";
    serviceState.innerHTML = "<span></span>服务正常";
  } catch {
    serviceState.className = "service-state is-error";
    serviceState.innerHTML = "<span></span>服务异常";
  }
}

function clearAuthenticatedState() {
  state.accessToken = "";
  state.accessTokenExpiresAt = 0;
  state.currentUser = null;
  state.documents = [];
  state.jobs = [];
}

async function finishAuthenticatedSession() {
  state.currentUser = await requestJson("/auth/me");
  state.authRecoveryNoticeShown = false;
  updateIdentityUI();
  await Promise.all([loadDocuments(), loadJobs()]);
}

async function startOidcLogin() {
  if (!state.oidcAuthManager) {
    throw new Error("Entra 登录配置不完整");
  }
  sessionStorage.setItem("knowledge.oidc.return_hash", location.hash || "#research");
  await state.oidcAuthManager.login();
}

async function logoutOidc() {
  const manager = state.oidcAuthManager;
  clearAuthenticatedState();
  updateSignedOutUI();
  await manager.logout();
}

async function login({ quiet = false } = {}) {
  closeDetailDrawer();
  const userId = $("#userId").value;
  const department = $("#department").value;
  const loginButton = $("#loginBtn");
  loginButton.disabled = true;
  $("#authStatus").className = "form-note";
  $("#authStatus").textContent = "正在验证身份";
  try {
    const token = await requestJson("/auth/dev-token", {
      method: "POST",
      authenticated: false,
      body: JSON.stringify({
        user_id: userId,
        department_ids: [department],
        role_ids: userId === "u_admin" ? ["admin"] : []
      })
    });
    state.accessToken = token.access_token;
    await finishAuthenticatedSession();
    $("#authStatus").textContent = `目录身份：${state.currentUser.identity_source}`;
    $("#identityPopover").hidden = true;
    $("#identityButton").setAttribute("aria-expanded", "false");
    if (!quiet) showToast("身份已切换", `${userLabels[userId]} · ${scopeLabel()}`);
  } catch (error) {
    $("#authStatus").className = "form-note is-error";
    $("#authStatus").textContent = error.message;
    $("#presenceDot").classList.remove("is-online");
    throw error;
  } finally {
    loginButton.disabled = false;
  }
}

async function initializeAuthentication() {
  state.authConfig = await requestJson("/auth/config", { authenticated: false });
  const isOidc = state.authConfig.mode === "oidc";
  if (isOidc && redirectToCanonicalOidcOrigin()) return;
  $("#oidcAuthPanel").hidden = !isOidc;
  $("#devAuthPanel").hidden = isOidc;
  if (!isOidc) {
    await login({ quiet: true });
    return;
  }
  $("#oidcAuthStatus").textContent = "使用企业账号访问知识库";
  state.oidcAuthManager = new OidcAuthManager(state.authConfig, {
    onAuthenticationRequired: handleAuthenticationRequired,
    onAuthenticated: finishPeerAuthenticatedSession
  });
  const authenticated = await state.oidcAuthManager.initialize();
  if (!authenticated) {
    updateSignedOutUI();
    return;
  }
  state.accessToken = await state.oidcAuthManager.getAccessToken();
  state.accessTokenExpiresAt = state.oidcAuthManager.accessTokenExpiresAt;
  const returnHash = sessionStorage.getItem("knowledge.oidc.return_hash");
  if (returnHash) {
    sessionStorage.removeItem("knowledge.oidc.return_hash");
    history.replaceState(null, "", `${location.pathname}${returnHash}`);
  }
  await finishAuthenticatedSession();
}

function handleAuthenticationRequired(error) {
  state.oidcAuthManager?.clearAccessToken();
  clearAuthenticatedState();
  if (state.authConfig?.mode === "oidc") updateSignedOutUI();
  if (state.authRecoveryNoticeShown) return;
  state.authRecoveryNoticeShown = true;
  showToast("登录会话已过期", error?.message || "请重新登录", "error");
}

async function finishPeerAuthenticatedSession(token) {
  state.accessToken = token;
  state.accessTokenExpiresAt = state.oidcAuthManager.accessTokenExpiresAt;
  try {
    await finishAuthenticatedSession();
  } catch (error) {
    handleAuthenticationRequired(error);
  }
}

function redirectToCanonicalOidcOrigin() {
  const redirectUri = state.authConfig?.redirect_uri;
  if (!redirectUri) return false;
  const canonical = new URL(redirectUri);
  if (canonical.origin === location.origin) return false;
  canonical.search = location.search;
  canonical.hash = location.hash || "#research";
  location.replace(canonical.toString());
  return true;
}

function updateIdentityUI() {
  const user = state.currentUser;
  if (!user) return;
  const label = user.display_name || userLabels[user.user_id] || user.user_id;
  const initial = label.trim().slice(0, 1).toUpperCase();
  $("#sidebarUser").textContent = label;
  $("#sidebarScope").textContent = scopeLabel();
  $("#sidebarAvatar").textContent = initial;
  $("#topbarAvatar").textContent = initial;
  $("#topbarUser").textContent = label;
  $("#topbarDepartment").textContent = scopeLabel();
  $("#presenceDot").classList.add("is-online");
  $("#researchScope").textContent = scopeLabel();
  const primaryDepartment = user.department_ids[0] || $("#department").value;
  $("#uploadDepartment").value = primaryDepartment;
  updatePermissionHint();
  if (state.authConfig?.mode === "oidc") {
    $("#oidcSignedIn").hidden = false;
    $("#oidcAvatar").textContent = initial;
    $("#oidcUserName").textContent = label;
    $("#oidcUserEmail").textContent = user.email || user.user_id;
    $("#oidcLoginBtn").hidden = true;
    $("#logoutBtn").hidden = false;
    $("#oidcAuthStatus").textContent = `目录身份：${user.identity_source}`;
  }
}

function updateSignedOutUI() {
  $("#sidebarUser").textContent = "尚未登录";
  $("#sidebarScope").textContent = "Microsoft Entra ID";
  $("#sidebarAvatar").textContent = "E";
  $("#topbarAvatar").textContent = "E";
  $("#topbarUser").textContent = "登录";
  $("#topbarDepartment").textContent = "Entra ID";
  $("#researchScope").textContent = "未认证";
  $("#presenceDot").classList.remove("is-online");
  $("#oidcSignedIn").hidden = true;
  $("#oidcLoginBtn").hidden = false;
  $("#logoutBtn").hidden = true;
  $("#oidcAuthStatus").textContent = "使用企业账号访问知识库";
}

function scopeLabel() {
  if (!state.currentUser) return "未认证";
  const departments = state.currentUser.department_ids || [];
  const roles = state.currentUser.role_ids || [];
  if (roles.includes("admin")) return "管理员";
  return departments.join(", ") || "无部门";
}

async function loadDocuments() {
  if (!state.accessToken) return;
  const container = $("#documents");
  if (container) container.innerHTML = '<div class="empty-row">正在加载文档</div>';
  try {
    state.documents = await requestJson("/documents");
    updateDocumentCounts();
    renderDocuments();
  } catch (error) {
    if (container) container.innerHTML = `<div class="empty-row">${escapeHtml(error.message)}</div>`;
  }
}

function updateDocumentCounts() {
  const count = state.documents.length;
  $("#navDocumentCount").textContent = String(count);
  $("#researchDocumentCount").textContent = String(count);
  $("#metricDocuments").textContent = String(count);
}

function renderDocuments() {
  const container = $("#documents");
  if (!container) return;
  const query = $("#documentSearch").value.trim().toLowerCase();
  const filtered = state.documents.filter((doc) =>
    [doc.title, doc.source_type, doc.department_id, doc.owner_id, doc.document_id]
      .some((value) => String(value || "").toLowerCase().includes(query))
  );
  if (!filtered.length) {
    container.innerHTML = '<div class="empty-row">没有匹配的文档</div>';
    return;
  }
  container.innerHTML = filtered.map((doc) => `
    <div class="table-row document-grid">
      <div class="doc-title">
        <span class="file-icon"><i data-lucide="${documentIcon(doc.source_type)}"></i></span>
        <div><strong>${escapeHtml(doc.title)}</strong><span>${escapeHtml(doc.document_id)}</span></div>
      </div>
      <span class="type-label">${escapeHtml(doc.source_type)}</span>
      <span class="table-secondary">${escapeHtml(doc.department_id || "未设置")}</span>
      <span class="table-secondary">${escapeHtml(doc.owner_id || "-")}</span>
      <button class="icon-button row-action" type="button" title="查看文档详情" aria-label="查看文档详情" data-open-document="${escapeHtml(doc.document_id)}"><i data-lucide="eye"></i></button>
    </div>
  `).join("");
  $$('[data-open-document]').forEach((button) => button.addEventListener("click", () => {
    openDocumentDetail(button.dataset.openDocument);
  }));
  refreshIcons();
}

function documentIcon(type) {
  if (type === "pdf") return "file-text";
  if (type === "word") return "file-type-2";
  if (type === "markdown") return "file-code-2";
  return "file";
}

async function queryKnowledge(questionText) {
  const question = (questionText || $("#question").value).trim();
  if (!question || state.querying) return;
  if (!state.accessToken) {
    $("#identityPopover").hidden = false;
    $("#identityButton").setAttribute("aria-expanded", "true");
    showToast("需要登录", "请先使用 Microsoft Entra ID 登录", "error");
    return;
  }
  state.querying = true;
  $("#queryBtn").disabled = true;
  $("#question").value = "";
  $("#conversationEmpty").hidden = true;
  appendUserMessage(question);
  const loadingMessage = appendLoadingMessage();
  resetEvidence();
  try {
    const data = state.queryMode === "deep"
      ? await runDeepResearch(question, loadingMessage)
      : await requestJson("/chat/query", {
        method: "POST",
        body: JSON.stringify({ question, limit: Number($("#resultLimit").value) })
      });
    renderAgentMessage(loadingMessage, data);
    renderCitations(data, loadingMessage);
  } catch (error) {
    renderAgentError(loadingMessage, error.message);
    showToast("问答失败", error.message, "error");
  } finally {
    state.querying = false;
    $("#queryBtn").disabled = false;
    $("#question").focus();
  }
}

async function runDeepResearch(question, message) {
  const job = await requestJson("/research/jobs", {
    method: "POST",
    body: JSON.stringify({
      question,
      per_query_limit: Number($("#resultLimit").value),
      max_rounds: 3
    })
  });
  state.researchJobId = job.job_id;
  updateResearchProgress(message, job);
  while (["queued", "running"].includes(job.status)) {
    await new Promise((resolve) => window.setTimeout(resolve, 1200));
    const latest = await requestJson(`/research/jobs/${encodeURIComponent(job.job_id)}`);
    Object.assign(job, latest);
    updateResearchProgress(message, job);
  }
  state.researchJobId = null;
  if (job.status === "cancelled") throw new Error("深度研究已取消");
  if (job.status !== "completed") throw new Error(job.error_message || "深度研究未完成");
  const answer = job.result?.answer;
  if (!answer) throw new Error("研究任务没有返回可用回答");
  answer.research = job.result?.research || null;
  return answer;
}

function updateResearchProgress(message, job) {
  const labels = {
    queued: "等待研究资源",
    starting: "初始化研究图",
    planning: "拆解复杂问题",
    checking_coverage: "检查证据覆盖度",
    expanding_queries: "补充检索问题",
    binding_citations: "绑定精确引用",
    verifying_evidence: "验证结论与证据"
  };
  const stage = job.stage?.startsWith("retrieving_round_")
    ? `第 ${job.stage.split("_").pop()} 轮混合检索`
    : (labels[job.stage] || "执行深度研究");
  message.innerHTML = `
    <div class="message-meta"><span class="agent-symbol"><i data-lucide="workflow"></i></span><strong>研究 Agent</strong></div>
    <div class="research-progress-line"><span>${escapeHtml(stage)}</span><strong>${Number(job.progress || 0)}%</strong></div>
    <div class="research-progress-track"><span style="width:${Math.max(2, Math.min(100, Number(job.progress || 0)))}%"></span></div>
    <button class="research-cancel" type="button" title="取消研究" aria-label="取消研究"><i data-lucide="square"></i><span>取消</span></button>
  `;
  const cancel = message.querySelector(".research-cancel");
  if (cancel) cancel.addEventListener("click", async () => {
    cancel.disabled = true;
    await requestJson(`/research/jobs/${encodeURIComponent(job.job_id)}/cancel`, { method: "POST" });
  });
  refreshIcons();
  scrollConversation();
}

function appendUserMessage(text) {
  const message = document.createElement("div");
  message.className = "message message-user";
  const body = document.createElement("div");
  body.className = "message-body";
  body.textContent = text;
  message.appendChild(body);
  $("#conversation").appendChild(message);
  scrollConversation();
}

function appendLoadingMessage() {
  const message = document.createElement("div");
  message.className = "message message-agent";
  message.innerHTML = `
    <div class="message-meta"><span class="agent-symbol"><i data-lucide="library-big"></i></span><strong>知识库 Agent</strong></div>
    <div class="typing-line" aria-label="正在研究"><span></span><span></span><span></span></div>
  `;
  $("#conversation").appendChild(message);
  refreshIcons();
  scrollConversation();
  return message;
}

function renderAgentMessage(message, data) {
  message.innerHTML = "";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.innerHTML = data.research
    ? '<span class="agent-symbol"><i data-lucide="workflow"></i></span><strong>研究 Agent</strong>'
    : '<span class="agent-symbol"><i data-lucide="library-big"></i></span><strong>知识库 Agent</strong>';
  const body = document.createElement("div");
  body.className = `message-body${data.verified ? "" : " is-error"}`;
  body.textContent = data.answer || data.refusal_reason || "未生成可验证回答";
  const verification = document.createElement("div");
  verification.className = "verification-line";
  verification.innerHTML = data.verified
    ? `<i data-lucide="badge-check"></i><span>证据验证通过 · ${data.citations.length} 条引用</span>`
    : '<i data-lucide="shield-alert"></i><span>证据不足或验证未通过</span>';
  message.append(meta, body, verification);
  if (data.research) {
    const summary = document.createElement("div");
    summary.className = "research-summary";
    summary.textContent = `${data.research.rounds} 轮检索 · ${data.research.evidence_count} 条证据 · 覆盖度 ${Math.round((data.research.coverage || 0) * 100)}%`;
    message.appendChild(summary);
  }
  refreshIcons();
  scrollConversation();
}

function renderAgentError(message, errorText) {
  message.innerHTML = `
    <div class="message-meta"><span class="agent-symbol"><i data-lucide="library-big"></i></span><strong>知识库 Agent</strong></div>
    <div class="message-body is-error">${escapeHtml(errorText)}</div>
  `;
  refreshIcons();
}

function renderCitations(data, message) {
  state.lastAnswer = data;
  if (!data.citations.length) return;
  const evidenceByChunk = new Map((data.evidence || []).map((item) => [item.chunk_id, item]));
  const section = document.createElement("section");
  section.className = "inline-evidence";
  section.innerHTML = `
    <div class="inline-evidence-heading">
      <div><i data-lucide="quote"></i><strong>引用证据</strong></div>
      <span>${data.citations.length} 条</span>
    </div>
    <div class="citation-list">${data.citations.map((item, index) => {
    const evidence = evidenceByChunk.get(item.chunk_id) || {};
    const score = evidence.reranker_score ?? evidence.score ?? 0;
    return `
      <button class="citation-card" type="button" data-citation-index="${index}">
        <div class="citation-index"><span>引用 ${index + 1}</span><span>${Math.round(score * 100)}% 匹配</span></div>
        <h3>${escapeHtml(item.title)}</h3>
        <p>第 ${escapeHtml(item.page)} 页 · ${escapeHtml(item.section_path || "正文")}</p>
        <p>${escapeHtml(item.chunk_id)}</p>
        <div class="score-bar"><span style="width:${Math.max(4, Math.min(100, score * 100))}%"></span></div>
        <i data-lucide="chevron-right"></i>
      </button>
    `;
  }).join("")}</div>`;
  message.appendChild(section);
  section.querySelectorAll('[data-citation-index]').forEach((card) => card.addEventListener("click", () => {
    section.querySelectorAll('[data-citation-index]').forEach((item) => item.classList.remove("is-active"));
    card.classList.add("is-active");
    openCitationPreview(data, Number(card.dataset.citationIndex));
  }));
  refreshIcons();
  scrollConversation();
}

function resetResearch() {
  $("#conversation").innerHTML = `
    <div class="empty-state" id="conversationEmpty">
      <div class="empty-icon"><i data-lucide="scan-search"></i></div>
      <h2>从知识库开始提问</h2>
      <div class="prompt-grid">
        <button class="prompt-option" type="button">企业知识库支持哪些能力？<i data-lucide="arrow-up-right"></i></button>
        <button class="prompt-option" type="button">产品资料中的引用策略是什么？<i data-lucide="arrow-up-right"></i></button>
        <button class="prompt-option" type="button">总结当前可访问的公司制度<i data-lucide="arrow-up-right"></i></button>
      </div>
    </div>`;
  bindPromptOptions();
  resetEvidence();
  refreshIcons();
}

function resetEvidence() {
  state.lastAnswer = null;
}

function scrollConversation() {
  const conversation = $("#conversation");
  window.requestAnimationFrame(() => { conversation.scrollTop = conversation.scrollHeight; });
}

function openDetailDrawer(eyebrow, title) {
  releasePdfObjectUrl();
  if (state.detailCloseTimer) window.clearTimeout(state.detailCloseTimer);
  $("#detailEyebrow").textContent = eyebrow;
  $("#detailTitle").textContent = title;
  $("#detailBody").innerHTML = '<div class="detail-loading"><i data-lucide="loader-circle"></i><span>正在读取</span></div>';
  const drawer = $("#detailDrawer");
  const scrim = $("#detailScrim");
  scrim.hidden = false;
  drawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("detail-open");
  window.requestAnimationFrame(() => {
    drawer.classList.add("is-open");
    scrim.classList.add("is-visible");
  });
  refreshIcons();
}

function closeDetailDrawer() {
  state.detailRequest += 1;
  const drawer = $("#detailDrawer");
  const scrim = $("#detailScrim");
  if (!drawer || !scrim) return;
  drawer.classList.remove("is-open");
  scrim.classList.remove("is-visible");
  drawer.setAttribute("aria-hidden", "true");
  document.body.classList.remove("detail-open");
  releasePdfObjectUrl();
  state.detailCloseTimer = window.setTimeout(() => { scrim.hidden = true; }, 220);
}

function releasePdfObjectUrl() {
  if (!state.pdfObjectUrl) return;
  URL.revokeObjectURL(state.pdfObjectUrl);
  state.pdfObjectUrl = null;
}

function renderDetailError(message) {
  $("#detailBody").innerHTML = `<div class="detail-error"><i data-lucide="circle-alert"></i><strong>无法读取详情</strong><span>${escapeHtml(message)}</span></div>`;
  refreshIcons();
}

async function openDocumentDetail(documentId) {
  const requestId = state.detailRequest + 1;
  state.detailRequest = requestId;
  openDetailDrawer("文档详情", "正在加载");
  try {
    const [documentDetail, versions] = await Promise.all([
      requestJson(`/documents/${encodeURIComponent(documentId)}`),
      requestJson(`/documents/${encodeURIComponent(documentId)}/versions`)
    ]);
    if (requestId !== state.detailRequest) return;
    $("#detailTitle").textContent = documentDetail.title;
    $("#detailBody").innerHTML = renderDocumentDetail(documentDetail, versions);
    refreshIcons();
  } catch (error) {
    if (requestId === state.detailRequest) renderDetailError(error.message);
  }
}

function renderDocumentDetail(documentDetail, versions) {
  const current = documentDetail.current_version;
  const filename = documentDetail.metadata?.filename || "-";
  return `
    <div class="detail-lead">
      <span class="file-icon"><i data-lucide="${documentIcon(documentDetail.source_type)}"></i></span>
      <div><h3>${escapeHtml(documentDetail.title)}</h3><p>${escapeHtml(filename)}</p></div>
    </div>
    <section class="detail-section">
      <div class="detail-section-heading"><h3>基本信息</h3><span>${escapeHtml(documentDetail.source_type.toUpperCase())}</span></div>
      <div class="detail-meta-grid">
        <div><span>文档 ID</span><strong>${escapeHtml(documentDetail.document_id)}</strong></div>
        <div><span>归属部门</span><strong>${escapeHtml(documentDetail.department_id || "未设置")}</strong></div>
        <div><span>所有者</span><strong>${escapeHtml(documentDetail.owner_id || "-")}</strong></div>
        <div><span>创建时间</span><strong>${escapeHtml(formatDate(documentDetail.created_at))}</strong></div>
      </div>
    </section>
    <section class="detail-section">
      <div class="detail-section-heading"><h3>当前版本</h3><span>${current ? `v${escapeHtml(current.version_number)}` : "无版本"}</span></div>
      ${current ? `
        <div class="detail-meta-grid">
          <div><span>版本 ID</span><strong>${escapeHtml(current.version_id)}</strong></div>
          <div><span>建立时间</span><strong>${escapeHtml(formatDate(current.created_at))}</strong></div>
          <div style="grid-column:1/-1"><span>内容指纹</span><strong class="hash-value">${escapeHtml(current.content_hash)}</strong></div>
        </div>` : '<p class="table-secondary">当前没有可用版本</p>'}
    </section>
    <section class="detail-section">
      <div class="detail-section-heading"><h3>版本记录</h3><span>${versions.length} 个版本</span></div>
      <div class="version-list">
        ${versions.map((version) => `
          <div class="version-row">
            <span class="version-number">v${escapeHtml(version.version_number)}</span>
            <div><strong>${escapeHtml(version.version_id)}</strong><span>${escapeHtml(formatDate(version.created_at))} · ${escapeHtml(version.content_hash.slice(0, 12))}</span></div>
            ${version.is_current ? '<span class="current-tag">当前</span>' : ""}
          </div>`).join("") || '<div class="empty-row">没有版本记录</div>'}
      </div>
    </section>`;
}

async function openCitationPreview(answer, citationIndex) {
  const citation = answer?.citations?.[citationIndex];
  if (!citation) return;
  const requestId = state.detailRequest + 1;
  state.detailRequest = requestId;
  openDetailDrawer(`引用 ${citationIndex + 1}`, citation.title);
  try {
    const chunk = await requestJson(`/chunks/${encodeURIComponent(citation.chunk_id)}`);
    if (requestId !== state.detailRequest) return;
    if (chunk.version_id !== citation.version_id) throw new Error("引用版本与原文版本不一致");
    const claims = (answer.claims || []).filter((claim) =>
      (claim.citation_chunk_ids || []).includes(citation.chunk_id)
    );
    let pdfPreview = null;
    let pdfPreviewError = "";
    if (chunk.document.source_type === "pdf") {
      try {
        pdfPreview = await requestJson(`/chunks/${encodeURIComponent(citation.chunk_id)}/preview`);
      } catch (error) {
        pdfPreviewError = error.message;
      }
    }
    if (requestId !== state.detailRequest) return;
    $("#detailBody").innerHTML = renderCitationDetail(chunk, claims, pdfPreview, pdfPreviewError);
    refreshIcons();
    if (pdfPreview) loadPdfPreviewImage(pdfPreview, requestId);
  } catch (error) {
    if (requestId === state.detailRequest) renderDetailError(error.message);
  }
}

function renderCitationDetail(chunk, claims, pdfPreview = null, pdfPreviewError = "") {
  const version = chunk.version;
  return `
    <div class="detail-lead">
      <span class="file-icon"><i data-lucide="${documentIcon(chunk.document.source_type)}"></i></span>
      <div><h3>${escapeHtml(chunk.document.title)}</h3><p>${escapeHtml(chunk.document.document_id)}</p></div>
    </div>
    ${claims.length ? `
      <section class="detail-section">
        <div class="detail-section-heading"><h3>绑定结论</h3><span>${claims.length} 条</span></div>
        <div class="claim-list">${claims.map((claim) => `<blockquote class="bound-claim">${escapeHtml(claim.text)}</blockquote>`).join("")}</div>
      </section>` : ""}
    ${renderPdfPagePreview(chunk, pdfPreview, pdfPreviewError)}
    <section class="detail-section">
      <div class="detail-section-heading"><h3>${chunk.document.source_type === "pdf" ? "解析文本" : "引用原文"}</h3><span>完整 Chunk</span></div>
      <div class="source-location">
        <span>第 ${escapeHtml(chunk.page)} 页</span>
        <span>${escapeHtml(chunk.section_path || "正文")}</span>
        <span>v${escapeHtml(version?.version_number || "-")}</span>
      </div>
      <p class="source-content">${renderSourceWithHighlight(chunk.content, claims)}</p>
    </section>
    <section class="detail-section">
      <div class="detail-section-heading"><h3>引用定位</h3><span>${version?.is_current ? "当前版本" : "历史版本"}</span></div>
      <div class="detail-meta-grid">
        <div><span>Chunk ID</span><strong>${escapeHtml(chunk.chunk_id)}</strong></div>
        <div><span>Version ID</span><strong>${escapeHtml(chunk.version_id)}</strong></div>
        <div style="grid-column:1/-1"><span>内容指纹</span><strong class="hash-value">${escapeHtml(chunk.content_hash)}</strong></div>
      </div>
    </section>`;
}

function renderPdfPagePreview(chunk, preview, errorMessage) {
  if (chunk.document.source_type !== "pdf") return "";
  if (!preview) {
    return `
      <section class="detail-section">
        <div class="detail-section-heading"><h3>PDF 原页</h3><span>第 ${escapeHtml(chunk.page)} 页</span></div>
        <div class="pdf-preview-error"><i data-lucide="file-warning"></i><span>${escapeHtml(errorMessage || "PDF 页面暂时不可用")}</span></div>
      </section>`;
  }
  const width = Math.max(1, Number(preview.page_width) || 1);
  const height = Math.max(1, Number(preview.page_height) || 1);
  const matchLabel = {
    "docling-bbox": "版面坐标定位",
    "text-search": "原文精确匹配",
    "page-only": "页级定位"
  }[preview.match_method] || "页级定位";
  return `
    <section class="detail-section pdf-page-section">
      <div class="detail-section-heading"><h3>PDF 原页</h3><span>第 ${escapeHtml(preview.page)} / ${escapeHtml(preview.page_count)} 页 · ${matchLabel}</span></div>
      <div class="pdf-page-stage" style="aspect-ratio:${width}/${height}">
        <div class="pdf-page-loading"><i data-lucide="loader-circle"></i><span>正在渲染页面</span></div>
        <img id="pdfPageImage" alt="${escapeHtml(chunk.document.title)} 第 ${escapeHtml(preview.page)} 页" hidden>
        <div class="pdf-highlight-layer" aria-hidden="true">
          ${(preview.highlights || []).map((box) => `
            <span class="pdf-highlight" style="left:${clampPercent(box.x)}%;top:${clampPercent(box.y)}%;width:${clampPercent(box.width)}%;height:${clampPercent(box.height)}%"></span>
          `).join("")}
        </div>
      </div>
    </section>`;
}

async function loadPdfPreviewImage(preview, requestId) {
  try {
    const blob = await requestBlob(preview.page_image_url);
    if (requestId !== state.detailRequest) return;
    releasePdfObjectUrl();
    state.pdfObjectUrl = URL.createObjectURL(blob);
    const image = $("#pdfPageImage");
    if (!image) return;
    image.addEventListener("load", () => {
      image.hidden = false;
      image.closest(".pdf-page-stage")?.classList.add("is-loaded");
    }, { once: true });
    image.addEventListener("error", () => {
      const loading = image.closest(".pdf-page-stage")?.querySelector(".pdf-page-loading");
      if (loading) loading.innerHTML = '<span>页面图像加载失败</span>';
    }, { once: true });
    image.src = state.pdfObjectUrl;
  } catch (error) {
    if (requestId !== state.detailRequest) return;
    const loading = $(".pdf-page-loading");
    if (loading) loading.innerHTML = `<i data-lucide="circle-alert"></i><span>${escapeHtml(error.message)}</span>`;
    refreshIcons();
  }
}

function clampPercent(value) {
  return Math.max(0, Math.min(100, Number(value || 0) * 100));
}

function renderSourceWithHighlight(content, claims) {
  const source = String(content || "");
  const keySentence = findKeySentence(source, claims.map((claim) => claim.text).join(" "));
  if (!keySentence) return escapeHtml(source);
  const start = source.indexOf(keySentence);
  if (start < 0) return escapeHtml(source);
  return `${escapeHtml(source.slice(0, start))}<mark>${escapeHtml(keySentence)}</mark>${escapeHtml(source.slice(start + keySentence.length))}`;
}

function findKeySentence(content, claimText) {
  if (!claimText.trim()) return "";
  const sentences = content.match(/[^。！？.!?\n]+(?:[。！？.!?]+|$)/g) || [];
  const claimTerms = new Set(extractMatchTerms(claimText));
  let bestSentence = "";
  let bestScore = 0;
  sentences.forEach((sentence) => {
    const terms = new Set(extractMatchTerms(sentence));
    const score = Array.from(terms).filter((term) => claimTerms.has(term)).length;
    if (score > bestScore) {
      bestScore = score;
      bestSentence = sentence.trim();
    }
  });
  return bestScore > 0 ? bestSentence : "";
}

function extractMatchTerms(text) {
  const common = new Set(Array.from("的一是在和与或及为有于中以了该本将可并对从等"));
  return (String(text).toLowerCase().match(/[\u3400-\u9fff]|[a-z0-9]{2,}/g) || [])
    .filter((term) => !common.has(term));
}

function setUploadMode(mode) {
  state.uploadMode = mode;
  $$('[data-upload-mode]').forEach((button) => button.classList.toggle("is-active", button.dataset.uploadMode === mode));
  $("#fileMode").hidden = mode !== "file";
  $("#textMode").hidden = mode !== "text";
}

function selectFile(file) {
  if (!file) return;
  state.selectedFile = file;
  $("#fileLabel").textContent = file.name;
  $("#filename").value = file.name;
  if (!$("#title").value || $("#title").value === "报销制度") {
    $("#title").value = file.name.replace(/\.[^.]+$/, "");
  }
}

async function uploadDocument() {
  const button = $("#uploadBtn");
  const department = $("#uploadDepartment").value;
  button.disabled = true;
  setUploadProgress("正在提交", 2, true);
  try {
    let upload;
    if (state.uploadMode === "file") {
      if (!state.selectedFile) throw new Error("请先选择一个文档");
      const form = new FormData();
      form.append("file", state.selectedFile);
      form.append("title", $("#title").value.trim());
      form.append("department_id", department);
      form.append("acl_departments", department);
      upload = await requestJson("/documents/upload-file", { method: "POST", body: form });
    } else {
      const content = $("#content").value.trim();
      if (!content) throw new Error("文档内容不能为空");
      upload = await requestJson("/documents/upload", {
        method: "POST",
        body: JSON.stringify({
          filename: $("#filename").value.trim() || "document.md",
          title: $("#title").value.trim() || null,
          department_id: department,
          acl_departments: [department],
          content_text: content
        })
      });
    }
    const job = await waitForJob(upload.job.job_id);
    setUploadProgress(`索引完成 · ${job.result.chunk_count || 0} 个分块`, 100, true);
    showToast("文档索引完成", $("#title").value.trim());
    await Promise.all([loadDocuments(), loadJobs()]);
  } catch (error) {
    setUploadProgress(error.message, 100, true);
    showToast("上传失败", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function waitForJob(jobId) {
  while (true) {
    const job = await requestJson(`/jobs/${encodeURIComponent(jobId)}`);
    setUploadProgress(`${statusLabels[job.status] || job.status}`, job.progress, true);
    if (job.status === "completed") return job;
    if (["failed", "cancelled"].includes(job.status)) {
      throw new Error(job.error_message || `索引任务${statusLabels[job.status] || job.status}`);
    }
    await new Promise((resolve) => window.setTimeout(resolve, 900));
  }
}

function setUploadProgress(label, percent, visible) {
  $("#uploadProgress").hidden = !visible;
  $("#uploadStatus").textContent = label;
  $("#uploadPercent").textContent = `${Math.round(percent)}%`;
  $("#uploadProgressBar").style.width = `${Math.max(0, Math.min(100, percent))}%`;
}

async function loadJobs() {
  if (!state.accessToken) return;
  const container = $("#jobs");
  if (container) container.innerHTML = '<div class="empty-row">正在加载任务</div>';
  try {
    state.jobs = await requestJson("/jobs");
    const activeCount = state.jobs.filter((job) => ["queued", "running"].includes(job.status)).length;
    $("#navJobCount").textContent = String(activeCount);
    $("#metricJobs").textContent = String(activeCount);
    renderJobs();
  } catch (error) {
    if (container) container.innerHTML = `<div class="empty-row">${escapeHtml(error.message)}</div>`;
  }
}

function renderJobs() {
  const container = $("#jobs");
  if (!container) return;
  if (!state.jobs.length) {
    container.innerHTML = '<div class="empty-row">暂无索引任务</div>';
    return;
  }
  container.innerHTML = state.jobs.map((job) => `
    <div class="table-row job-grid">
      <div class="job-title">
        <span class="file-icon"><i data-lucide="binary"></i></span>
        <div><strong>${escapeHtml(job.document_id)}</strong><span>${escapeHtml(job.job_id)}</span></div>
      </div>
      <span class="status-pill status-${escapeHtml(job.status)}">${escapeHtml(statusLabels[job.status] || job.status)}</span>
      <div class="table-progress"><div class="progress-track"><span style="width:${job.progress}%"></span></div><span>${job.progress}%</span></div>
      <span class="table-secondary">${formatDate(job.updated_at || job.created_at)}</span>
      ${jobAction(job)}
    </div>
  `).join("");
  $$('[data-job-action]').forEach((button) => button.addEventListener("click", () => runJobAction(button.dataset.jobAction, button.dataset.jobId)));
  refreshIcons();
}

function jobAction(job) {
  if (job.status === "failed" || job.status === "cancelled") {
    return `<button class="icon-button row-action" type="button" title="重试" aria-label="重试任务" data-job-action="retry" data-job-id="${escapeHtml(job.job_id)}"><i data-lucide="rotate-ccw"></i></button>`;
  }
  if (job.status === "queued" || job.status === "running") {
    return `<button class="icon-button row-action" type="button" title="取消" aria-label="取消任务" data-job-action="cancel" data-job-id="${escapeHtml(job.job_id)}"><i data-lucide="square"></i></button>`;
  }
  return '<span></span>';
}

async function runJobAction(action, jobId) {
  try {
    await requestJson(`/jobs/${encodeURIComponent(jobId)}/${action}`, { method: "POST" });
    showToast(action === "retry" ? "任务已重新排队" : "任务已取消");
    await loadJobs();
  } catch (error) {
    showToast("任务操作失败", error.message, "error");
  }
}

async function loadSystemStatus() {
  $("#systemTimestamp").textContent = "正在检查";
  try {
    const pipeline = await requestJson("/admin/pipeline");
    renderPipelineStatus(pipeline);
    $("#systemHeadline").textContent = "服务运行正常";
  } catch (error) {
    renderPipelineStatus(null);
    $("#systemHeadline").textContent = error.message.includes("Admin") ? "管理员可查看完整管线" : "部分状态不可用";
  }
  $("#metricIdentity").textContent = state.currentUser?.identity_source || "未连接";
  $("#systemTimestamp").textContent = `检查于 ${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;
}

function renderPipelineStatus(pipeline) {
  const rows = pipeline ? [
    ["database", "知识存储", pipeline.store, "PostgreSQL / pgvector"],
    ["brain-circuit", "Embedding", pipeline.embedding, `${pipeline.embedding_dimensions} 维`],
    ["list-filter", "Reranker", pipeline.reranker, "混合检索重排"],
    ["file-scan", "文档解析", pipeline.pdf_parser, pipeline.object_storage],
    ["shield-check", "身份目录", pipeline.identity_directory, pipeline.identity_mode],
    ["workflow", "任务队列", pipeline.job_dispatcher, pipeline.auth_mode]
  ] : [
    ["database", "知识存储", "已连接", "详情需要管理员权限"],
    ["shield-check", "身份目录", state.currentUser?.identity_source || "已连接", scopeLabel()],
    ["workflow", "任务队列", "运行中", `${state.jobs.length} 个可见任务`]
  ];
  $("#pipelineStatus").innerHTML = rows.map(([icon, label, value, detail]) => `
    <div class="status-row">
      <div class="status-row-label"><i data-lucide="${icon}"></i><div><strong>${escapeHtml(label)}</strong><span>${escapeHtml(detail)}</span></div></div>
      <span class="status-value">${escapeHtml(value)}</span>
      <strong class="status-ok">正常</strong>
    </div>
  `).join("");
  refreshIcons();
}

function updatePermissionHint() {
  $("#permissionHint").textContent = `仅 ${$("#uploadDepartment").value} 部门可检索`;
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  })[char]);
}

function bindPromptOptions() {
  $$(".prompt-option").forEach((button) => button.addEventListener("click", () => queryKnowledge(button.textContent.trim())));
}

function bindEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  $$('[data-open-view]').forEach((button) => button.addEventListener("click", () => switchView(button.dataset.openView)));
  $("#mobileMenu").addEventListener("click", openMobileNav);
  $("#mobileScrim").addEventListener("click", closeMobileNav);
  $("#detailClose").addEventListener("click", closeDetailDrawer);
  $("#detailScrim").addEventListener("click", closeDetailDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("#detailDrawer").classList.contains("is-open")) {
      closeDetailDrawer();
    }
  });
  $("#identityButton").addEventListener("click", () => {
    const popover = $("#identityPopover");
    popover.hidden = !popover.hidden;
    $("#identityButton").setAttribute("aria-expanded", String(!popover.hidden));
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest("#identityButton, #identityPopover")) {
      $("#identityPopover").hidden = true;
      $("#identityButton").setAttribute("aria-expanded", "false");
    }
  });
  $("#userId").addEventListener("change", () => {
    const map = { u_sales: "sales", u_hr: "hr", u_finance: "finance", u_admin: "platform" };
    $("#department").value = map[$("#userId").value] || "sales";
  });
  $("#loginBtn").addEventListener("click", () => login().catch(() => {}));
  $("#oidcLoginBtn").addEventListener("click", () => {
    startOidcLogin().catch((error) => showToast("无法开始登录", error.message, "error"));
  });
  $("#logoutBtn").addEventListener("click", () => {
    logoutOidc().catch((error) => showToast("退出失败", error.message, "error"));
  });
  $("#queryBtn").addEventListener("click", () => queryKnowledge());
  $$('[data-query-mode]').forEach((button) => button.addEventListener("click", () => {
    state.queryMode = button.dataset.queryMode;
    $$('[data-query-mode]').forEach((item) => item.classList.toggle("is-active", item === button));
    $("#composerNote").textContent = state.queryMode === "deep"
      ? "复杂问题将异步执行多轮检索、覆盖检查与引用验证"
      : "回答仅使用当前身份可访问的证据";
    $("#question").focus();
  }));
  $("#question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      queryKnowledge();
    }
  });
  $("#newResearchBtn").addEventListener("click", resetResearch);
  bindPromptOptions();
  $("#documentSearch").addEventListener("input", renderDocuments);
  $("#refreshDocuments").addEventListener("click", loadDocuments);
  $("#refreshJobs").addEventListener("click", loadJobs);
  $("#refreshSystem").addEventListener("click", loadSystemStatus);
  $$('[data-upload-mode]').forEach((button) => button.addEventListener("click", () => setUploadMode(button.dataset.uploadMode)));
  $("#fileInput").addEventListener("change", (event) => selectFile(event.target.files[0]));
  const dropzone = $("#dropzone");
  ["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-dragging");
  }));
  dropzone.addEventListener("drop", (event) => selectFile(event.dataTransfer.files[0]));
  $("#uploadDepartment").addEventListener("change", updatePermissionHint);
  $("#uploadBtn").addEventListener("click", uploadDocument);
}

async function initialize() {
  refreshIcons();
  bindEvents();
  await checkHealth();
  try {
    await initializeAuthentication();
  } catch (error) {
    showToast("身份连接失败", error.message, "error");
    if (state.authConfig?.mode === "oidc") updateSignedOutUI();
  }
  const initialView = location.hash.slice(1);
  if (viewNames[initialView]) switchView(initialView);
}

if (window.self === window.top) initialize();
