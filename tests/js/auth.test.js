import assert from "node:assert/strict";
import test from "node:test";

import {
  AuthenticationRequiredError,
  OidcAuthManager,
  isInteractionRequired
} from "../../backend/app/static/auth.js";

const account = { homeAccountId: "account-1", username: "user@example.com" };
const config = {
  client_id: "client-id",
  authority: "https://login.microsoftonline.com/tenant-id",
  redirect_uri: "https://kb.example.com/",
  post_logout_redirect_uri: "https://kb.example.com/",
  scope: "api://knowledge-api/access_as_user",
  token_refresh_skew_seconds: 120
};

class FakeClient {
  constructor({ redirectResponse = null, accounts = [], silentResponse = null, silentError = null } = {}) {
    this.redirectResponse = redirectResponse;
    this.accounts = accounts;
    this.silentResponse = silentResponse;
    this.silentError = silentError;
    this.activeAccount = null;
    this.silentRequests = [];
  }

  async initialize() {}
  async handleRedirectPromise() { return this.redirectResponse; }
  getActiveAccount() { return this.activeAccount; }
  getAllAccounts() { return this.accounts; }
  setActiveAccount(value) { this.activeAccount = value; }
  async acquireTokenSilent(request) {
    this.silentRequests.push(request);
    if (this.silentError) throw this.silentError;
    return this.silentResponse;
  }
  async ssoSilent(request) {
    this.ssoRequest = request;
    if (this.silentError) throw this.silentError;
    return this.silentResponse;
  }
}

function dependencies(client, extra = {}) {
  return {
    clientFactory: () => client,
    channelFactory: () => null,
    now: () => 1000,
    setTimer: () => 1,
    clearTimer: () => {},
    instanceId: "tab-1",
    ...extra
  };
}

test("redirect login is cached and a 401 forces one silent renewal", async () => {
  const redirectResponse = {
    account,
    accessToken: "initial-token",
    expiresOn: new Date(3601000)
  };
  const client = new FakeClient({
    redirectResponse,
    silentResponse: {
      account,
      accessToken: "refreshed-token",
      expiresOn: new Date(7201000)
    }
  });
  const manager = new OidcAuthManager(config, dependencies(client));

  assert.equal(await manager.initialize(), true);
  assert.equal(await manager.getAccessToken(), "initial-token");
  assert.equal(await manager.recoverFromUnauthorized(), "refreshed-token");
  assert.equal(client.silentRequests.length, 1);
  assert.equal(client.silentRequests[0].forceRefresh, true);
});

test("startup returns signed out when silent renewal needs interaction", async () => {
  const client = new FakeClient({
    accounts: [account],
    silentError: { errorCode: "login_required" }
  });
  const manager = new OidcAuthManager(config, dependencies(client));

  assert.equal(await manager.initialize(), false);
  await assert.rejects(
    () => manager.getAccessToken(),
    AuthenticationRequiredError
  );
});

test("tab coordination shares only an account hint, never a token", async () => {
  const messages = [];
  const channel = { postMessage: (message) => messages.push(message), onmessage: null };
  const client = new FakeClient({ redirectResponse: {
    account,
    accessToken: "secret-token",
    expiresOn: new Date(3601000)
  } });
  const manager = new OidcAuthManager(config, dependencies(client, {
    channelFactory: () => channel
  }));
  await manager.initialize();

  manager.handleChannelMessage({ type: "session-request", source: "tab-2" });

  const hint = messages.at(-1);
  assert.equal(hint.type, "session-hint");
  assert.equal(hint.loginHint, "user@example.com");
  assert.equal(JSON.stringify(hint).includes("secret-token"), false);
});

test("known Entra interaction errors are normalized", () => {
  assert.equal(isInteractionRequired({ errorCode: "consent_required" }), true);
  assert.equal(isInteractionRequired({ errorCode: "network_error" }), false);
});

test("an already-open signed-out tab restores after another tab signs in", async () => {
  let restoredToken = "";
  const client = new FakeClient({ silentResponse: {
    account,
    accessToken: "peer-token",
    expiresOn: new Date(3601000)
  } });
  const manager = new OidcAuthManager(config, dependencies(client, {
    onAuthenticated: (token) => { restoredToken = token; }
  }));
  manager.client = client;

  manager.handleChannelMessage({
    type: "signed-in",
    source: "tab-2",
    loginHint: "user@example.com"
  });
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(client.ssoRequest.loginHint, "user@example.com");
  assert.equal(restoredToken, "peer-token");
});
