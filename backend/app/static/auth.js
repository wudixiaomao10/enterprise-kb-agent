import {
  BrowserCacheLocation,
  InteractionRequiredAuthError,
  PublicClientApplication
} from "@azure/msal-browser";

const DEFAULT_REFRESH_SKEW_MS = 120000;
const PEER_RESPONSE_WAIT_MS = 350;

export class AuthenticationRequiredError extends Error {
  constructor(message = "登录会话已过期，请重新登录") {
    super(message);
    this.name = "AuthenticationRequiredError";
    this.code = "authentication_required";
  }
}

export class OidcAuthManager {
  constructor(config, dependencies = {}) {
    this.config = config;
    this.scopes = [config.scope].filter(Boolean);
    this.refreshSkewMs = Math.max(
      30000,
      Number(config.token_refresh_skew_seconds || DEFAULT_REFRESH_SKEW_MS / 1000) * 1000
    );
    this.clientFactory = dependencies.clientFactory
      || ((clientConfig) => new PublicClientApplication(clientConfig));
    this.channelFactory = dependencies.channelFactory
      || ((name) => typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel(name));
    this.now = dependencies.now || (() => Date.now());
    this.setTimer = dependencies.setTimer || ((callback, delay) => window.setTimeout(callback, delay));
    this.clearTimer = dependencies.clearTimer || ((timer) => window.clearTimeout(timer));
    this.onAuthenticationRequired = dependencies.onAuthenticationRequired || (() => {});
    this.onAuthenticated = dependencies.onAuthenticated || (() => {});
    this.instanceId = dependencies.instanceId || createInstanceId();
    this.client = null;
    this.channel = null;
    this.account = null;
    this.accessToken = "";
    this.accessTokenExpiresAt = 0;
    this.tokenPromise = null;
    this.refreshTimer = null;
    this.peerRestorePromise = null;
  }

  async initialize() {
    this.client = this.clientFactory({
      auth: {
        clientId: this.config.client_id,
        authority: this.config.authority,
        redirectUri: this.config.redirect_uri,
        postLogoutRedirectUri: this.config.post_logout_redirect_uri || this.config.redirect_uri,
        navigateToLoginRequestUrl: false
      },
      cache: {
        cacheLocation: BrowserCacheLocation.SessionStorage
      }
    });
    this.channel = this.channelFactory("knowledge.oidc.session");
    if (this.channel) this.channel.onmessage = (event) => this.handleChannelMessage(event.data);

    await this.client.initialize();
    const redirectResponse = await this.client.handleRedirectPromise();
    if (redirectResponse?.account) {
      this.acceptAuthenticationResult(redirectResponse);
      this.broadcastSession("signed-in");
      return true;
    }

    this.account = this.client.getActiveAccount() || this.client.getAllAccounts()[0] || null;
    if (this.account) {
      this.client.setActiveAccount(this.account);
      try {
        await this.getAccessToken();
        return true;
      } catch (error) {
        if (error instanceof AuthenticationRequiredError) {
          this.clearAccessToken();
          return false;
        }
        throw error;
      }
    }

    return this.restoreFromPeerTab();
  }

  async login(returnUrl = currentReturnUrl()) {
    await this.client.loginRedirect({
      scopes: this.scopes,
      redirectStartPage: returnUrl
    });
  }

  async logout() {
    this.broadcastSession("signed-out");
    this.clearAccessToken();
    const account = this.account;
    this.account = null;
    await this.client.logoutRedirect({
      account,
      postLogoutRedirectUri: this.config.post_logout_redirect_uri || this.config.redirect_uri
    });
  }

  async getAccessToken({ forceRefresh = false } = {}) {
    if (!this.account) throw new AuthenticationRequiredError();
    if (
      !forceRefresh
      && this.accessToken
      && this.accessTokenExpiresAt > this.now() + this.refreshSkewMs
    ) {
      return this.accessToken;
    }
    if (this.tokenPromise && !forceRefresh) return this.tokenPromise;

    const request = {
      account: this.account,
      scopes: this.scopes,
      forceRefresh
    };
    const tokenPromise = this.client.acquireTokenSilent(request)
      .then((response) => {
        this.acceptAuthenticationResult(response);
        return this.accessToken;
      })
      .catch((error) => {
        if (isInteractionRequired(error)) throw new AuthenticationRequiredError();
        throw error;
      })
      .finally(() => {
        if (this.tokenPromise === tokenPromise) this.tokenPromise = null;
      });
    this.tokenPromise = tokenPromise;
    return tokenPromise;
  }

  async recoverFromUnauthorized() {
    this.clearAccessToken();
    try {
      return await this.getAccessToken({ forceRefresh: true });
    } catch (error) {
      if (error instanceof AuthenticationRequiredError) {
        this.onAuthenticationRequired(error);
      }
      throw error;
    }
  }

  async clearPeerSession() {
    this.clearAccessToken();
    const account = this.account;
    this.account = null;
    if (account && this.client?.clearCache) {
      await this.client.clearCache({ account });
    }
    this.onAuthenticationRequired(new AuthenticationRequiredError("已在其他标签页退出登录"));
  }

  acceptAuthenticationResult(response) {
    if (!response?.accessToken) throw new AuthenticationRequiredError();
    if (response.account) {
      this.account = response.account;
      this.client.setActiveAccount(response.account);
    }
    this.accessToken = response.accessToken;
    const expiresAt = response.expiresOn instanceof Date
      ? response.expiresOn.getTime()
      : this.now() + 3600000;
    this.accessTokenExpiresAt = expiresAt;
    this.scheduleRefresh();
  }

  clearAccessToken() {
    this.accessToken = "";
    this.accessTokenExpiresAt = 0;
    if (this.refreshTimer !== null) this.clearTimer(this.refreshTimer);
    this.refreshTimer = null;
  }

  scheduleRefresh() {
    if (this.refreshTimer !== null) this.clearTimer(this.refreshTimer);
    const delay = Math.max(1000, this.accessTokenExpiresAt - this.now() - this.refreshSkewMs);
    this.refreshTimer = this.setTimer(async () => {
      this.refreshTimer = null;
      try {
        await this.getAccessToken({ forceRefresh: true });
      } catch (error) {
        if (error instanceof AuthenticationRequiredError) this.onAuthenticationRequired(error);
      }
    }, delay);
  }

  async restoreFromPeerTab() {
    if (!this.channel) return false;
    if (this.peerRestorePromise) return this.peerRestorePromise;
    this.peerRestorePromise = new Promise((resolve) => {
      const timer = this.setTimer(() => resolve(false), PEER_RESPONSE_WAIT_MS);
      this.pendingPeerRestore = async (loginHint) => {
        this.clearTimer(timer);
        try {
          await this.restoreWithLoginHint(loginHint);
          resolve(true);
        } catch {
          resolve(false);
        }
      };
      this.channel.postMessage({ type: "session-request", source: this.instanceId });
    }).finally(() => {
      this.pendingPeerRestore = null;
      this.peerRestorePromise = null;
    });
    return this.peerRestorePromise;
  }

  handleChannelMessage(message) {
    if (!message || message.source === this.instanceId) return;
    if (message.type === "signed-out") {
      this.clearPeerSession().catch(() => {});
      return;
    }
    if (message.type === "session-request" && this.account) {
      this.broadcastSession("session-hint");
      return;
    }
    if (["session-hint", "signed-in"].includes(message.type) && message.loginHint) {
      if (this.pendingPeerRestore) {
        const restore = this.pendingPeerRestore;
        this.pendingPeerRestore = null;
        restore(message.loginHint);
        return;
      }
      if (!this.accessToken) {
        this.restoreWithLoginHint(message.loginHint)
          .then(() => this.onAuthenticated(this.accessToken))
          .catch(() => {});
      }
    }
  }

  async restoreWithLoginHint(loginHint) {
    const response = await this.client.ssoSilent({
      scopes: this.scopes,
      loginHint
    });
    this.acceptAuthenticationResult(response);
    return this.accessToken;
  }

  broadcastSession(type) {
    if (!this.channel) return;
    this.channel.postMessage({
      type,
      source: this.instanceId,
      loginHint: type === "signed-out" ? undefined : this.account?.username
    });
  }
}

export function isInteractionRequired(error) {
  if (error instanceof InteractionRequiredAuthError) return true;
  return [
    "interaction_required",
    "login_required",
    "consent_required",
    "no_account_error"
  ].includes(String(error?.errorCode || error?.code || ""));
}

function currentReturnUrl() {
  if (typeof location === "undefined") return "";
  return `${location.origin}${location.pathname}${location.hash || "#research"}`;
}

function createInstanceId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export const TOKEN_REFRESH_SKEW_MS = DEFAULT_REFRESH_SKEW_MS;
