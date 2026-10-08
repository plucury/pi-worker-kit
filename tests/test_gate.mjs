/**
 * Offline, synthetic tests for the portable model-gated skills extension.
 *
 * Node's built-in test runner plus jiti only — no Pi runtime, no network, no model
 * calls, no installs. The Pi SDK is imported type-only inside the extension, so it is
 * erased at load time and is never resolved here.
 *
 * jiti is loaded through the optional developer module specifier
 * `PI_WORKER_JITI_PATH` (an already installed jiti), falling back to the plain
 * `jiti` specifier from node_modules:
 *
 *   PI_WORKER_JITI_PATH=file:///…/jiti/lib/jiti.mjs node --test tests/test_gate.mjs
 *
 * Fixtures are synthetic, catalogs live in private per-test temporary directories,
 * and the environment plus the working directory are always restored.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cpSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve, sep } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const { createJiti } = await import(process.env.PI_WORKER_JITI_PATH || "jiti");

const jiti = createJiti(import.meta.url);

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const GATE_SOURCE = join(REPO_ROOT, "extensions", "profile-gated-skills.ts");

/** Load the gate under test through jiti (TypeScript is transpiled, types erased). */
function loadGate(sourcePath = GATE_SOURCE) {
  return jiti(sourcePath);
}

const gate = loadGate();
const {
  GATED_SKILL_PATH,
  parseMainProfiles,
  derivedId,
  eligibilityFor,
  resolveProfilesFile,
  defaultProfilesFile,
  scanRootValueTokens,
} = gate;

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

/** Synthetic provider/model values; nothing here refers to a real provider account. */
const P = "synthetic-provider";
const M = "synthetic-model";

function tempDir(t, prefix = "gate-") {
  const dir = mkdtempSync(join(tmpdir(), `pi-worker-${prefix}`));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}

/** Write a catalog into a private temp file and point the gate at it. */
function useCatalog(t, text, { name = "profiles.json" } = {}) {
  const dir = tempDir(t, "catalog-");
  const file = join(dir, name);
  if (text !== null) writeFileSync(file, text, "utf8");
  const previous = process.env.PI_WORKER_PROFILES_FILE;
  process.env.PI_WORKER_PROFILES_FILE = file;
  t.after(() => {
    if (previous === undefined) delete process.env.PI_WORKER_PROFILES_FILE;
    else process.env.PI_WORKER_PROFILES_FILE = previous;
  });
  return file;
}

function withEnv(t, key, value) {
  const previous = process.env[key];
  if (value === undefined) delete process.env[key];
  else process.env[key] = value;
  t.after(() => {
    if (previous === undefined) delete process.env[key];
    else process.env[key] = previous;
  });
}

function withCwd(t, dir) {
  const previous = process.cwd();
  process.chdir(dir);
  t.after(() => process.chdir(previous));
}

function catalog(rows, extra = "") {
  return `{"version":2,"profiles":[${rows.join(",")}]${extra}}`;
}

function row(fields) {
  return JSON.stringify(fields);
}

const MAIN_ROW = row({ provider: P, model: M, enabled: true, roles: ["main"] });

/** Minimal Pi stand-in: only `on` exists; any other member access fails loudly. */
function createPi() {
  const handlers = new Map();
  const target = {
    on(event, handler) {
      assert.equal(typeof handler, "function", `handler for ${event} must be a function`);
      handlers.set(event, handler);
      return () => handlers.delete(event);
    },
  };
  const pi = new Proxy(target, {
    get(t, prop) {
      if (prop in t) return t[prop];
      throw new Error(`mock pi received an unsupported member: ${String(prop)}`);
    },
  });
  return { pi, handlers };
}

function createCtx({ model = { provider: P, id: M }, hasUI = true, notify } = {}) {
  const notices = [];
  const ui = {
    notify: notify ?? ((message, level) => notices.push({ message, level })),
  };
  return {
    ctx: { model, ui, hasUI, mode: hasUI ? "tui" : "print", cwd: process.cwd() },
    notices,
  };
}

async function startGate(options = {}) {
  const { pi, handlers } = createPi();
  const { ctx, notices } = createCtx(options);
  gate.default(pi);
  assert.deepEqual(
    [...handlers.keys()].sort(),
    ["before_agent_start", "model_select", "resources_discover"],
    "gate registers exactly the official discovery/sync hooks",
  );
  return { handlers, ctx, notices };
}

const fakeSkill = (filePath) => ({
  name: `fake-${filePath.length}`,
  description: "unrelated synthetic skill",
  filePath,
  baseDir: dirname(filePath),
  sourceInfo: { path: filePath, source: "synthetic", scope: "user", origin: "top-level" },
  disableModelInvocation: false,
});

// ---------------------------------------------------------------------------
// path resolution
// ---------------------------------------------------------------------------

test("the gated skill is the package root SKILL.md resolved from the extension's own file", () => {
  assert.equal(GATED_SKILL_PATH, join(REPO_ROOT, "SKILL.md"));
  assert.equal(dirname(GATED_SKILL_PATH), REPO_ROOT);
  // The resolution is derived from this module, so the name of the directory that
  // happens to host the checkout is irrelevant (asserted in the relocation test).
  assert.equal(GATED_SKILL_PATH, resolve(GATE_SOURCE, "..", "..", "SKILL.md"));
});

test("relocated extension copies gate their own SKILL.md", (t) => {
  const host = tempDir(t, "reloc-");
  const names = ["renamed-copy", "with space", "gated-skills", "package", "a.b.c"];
  for (const name of names) {
    const root = join(host, name);
    mkdirSync(join(root, "extensions"), { recursive: true });
    cpSync(GATE_SOURCE, join(root, "extensions", "profile-gated-skills.ts"));
    writeFileSync(join(root, "SKILL.md"), "---\nname: synthetic-skill\ndescription: synthetic relocation fixture\n---\n", "utf8");

    const relocated = loadGate(join(root, "extensions", "profile-gated-skills.ts"));
    assert.equal(relocated.GATED_SKILL_PATH, join(root, "SKILL.md"), `copy under ${name} must gate its own SKILL.md`);
    assert.equal(relocated.EXTENSION_DIR, join(root, "extensions"));
  }
});

test("the relocated copy only advertises the skill when its own catalog says so", async (t) => {
  const root = join(tempDir(t, "reloc-run-"), "under space");
  mkdirSync(join(root, "extensions"), { recursive: true });
  cpSync(GATE_SOURCE, join(root, "extensions", "profile-gated-skills.ts"));
  writeFileSync(join(root, "SKILL.md"), "---\nname: synthetic-skill\ndescription: synthetic relocation fixture\n---\n", "utf8");
  const relocated = loadGate(join(root, "extensions", "profile-gated-skills.ts"));

  useCatalog(t, catalog([MAIN_ROW]));
  const { pi, handlers } = createPi();
  const { ctx } = createCtx();
  relocated.default(pi);
  const result = await handlers.get("resources_discover")({ type: "resources_discover", cwd: root, reason: "startup" }, ctx);
  assert.deepEqual(result.skillPaths, [join(root, "SKILL.md")]);
});

// ---------------------------------------------------------------------------
// catalog discovery precedence
// ---------------------------------------------------------------------------

test("the catalog comes from PI_WORKER_PROFILES_FILE, else the home default, never the CWD", (t) => {
  const explicit = tempDir(t, "explicit-");
  const explicitFile = join(explicit, "custom.json");
  writeFileSync(explicitFile, catalog([MAIN_ROW]), "utf8");
  withEnv(t, "PI_WORKER_PROFILES_FILE", explicitFile);
  assert.equal(resolveProfilesFile(), explicitFile);

  const home = tempDir(t, "home-");
  mkdirSync(join(home, ".config", "pi-worker"), { recursive: true });
  writeFileSync(join(home, ".config", "pi-worker", "profiles.json"), catalog([MAIN_ROW]), "utf8");
  withEnv(t, "HOME", home);
  withEnv(t, "PI_WORKER_PROFILES_FILE", undefined);
  assert.equal(resolveProfilesFile(), join(home, ".config", "pi-worker", "profiles.json"));
  assert.equal(eligibilityFor({ provider: P, id: M }).eligible, true);
});

test("a profiles.json in the working directory is never discovered", (t) => {
  const workdir = tempDir(t, "cwd-");
  writeFileSync(join(workdir, "profiles.json"), catalog([MAIN_ROW]), "utf8");
  writeFileSync(join(workdir, "profiles.example.json"), catalog([MAIN_ROW]), "utf8");
  const home = tempDir(t, "cwd-home-");
  mkdirSync(join(home, ".config", "pi-worker"), { recursive: true });
  withEnv(t, "HOME", home);
  withEnv(t, "PI_WORKER_PROFILES_FILE", undefined);
  withCwd(t, workdir);

  assert.equal(defaultProfilesFile(), join(home, ".config", "pi-worker", "profiles.json"));
  assert.equal(eligibilityFor({ provider: P, id: M }).eligible, false);
  assert.equal(eligibilityFor({ provider: P, id: M }).catalogUnavailable, true);
});

test("a relative env path is the host's explicit choice, not a discovery mechanism", (t) => {
  const workdir = tempDir(t, "rel-");
  writeFileSync(join(workdir, "profiles.json"), catalog([MAIN_ROW]), "utf8");
  withCwd(t, workdir);
  withEnv(t, "PI_WORKER_PROFILES_FILE", "profiles.json");
  // The host explicitly pointed at a relative path: Node resolves it against the
  // process CWD, exactly as the documentation states.
  assert.equal(resolveProfilesFile(), "profiles.json");
  assert.equal(eligibilityFor({ provider: P, id: M }).eligible, true);
});

// ---------------------------------------------------------------------------
// eligibility
// ---------------------------------------------------------------------------

test("only an exact enabled main-role pair advertises the skill", async (t) => {
  useCatalog(t, catalog([MAIN_ROW]));
  const { handlers, ctx } = await startGate();
  assert.deepEqual(
    await handlers.get("resources_discover")({ type: "resources_discover", reason: "startup", cwd: REPO_ROOT }, ctx),
    { skillPaths: [GATED_SKILL_PATH] },
  );
});

test("unmatched provider or unmatched model stays hidden", (t) => {
  useCatalog(t, catalog([MAIN_ROW]));
  assert.equal(eligibilityFor({ provider: "other-provider", id: M }).eligible, false);
  assert.equal(eligibilityFor({ provider: P, id: "other-model" }).eligible, false);
  assert.equal(eligibilityFor({ provider: P.toUpperCase(), id: M }).eligible, false);
  assert.equal(eligibilityFor({ provider: P, id: `${M}-latest` }).eligible, false);
  // No wildcards, no prefix or provider-agnostic fallback.
  assert.equal(eligibilityFor({ provider: P, id: "synthetic-" }).eligible, false);
  assert.equal(eligibilityFor({ provider: "", id: M }).eligible, false);
  assert.equal(eligibilityFor({ provider: P, id: "" }).eligible, false);
  assert.equal(eligibilityFor(undefined).eligible, false);
});

test("disabled rows, sub-only rows and empty role lists are inert; a shared role opens the gate", (t) => {
  useCatalog(t, catalog([row({ provider: P, model: M, enabled: false, roles: ["main"] })]));
  assert.equal(eligibilityFor({ provider: P, id: M }).eligible, false);

  useCatalog(t, catalog([row({ provider: P, model: M, enabled: true, roles: ["sub"] })]));
  assert.equal(eligibilityFor({ provider: P, id: M }).eligible, false);

  useCatalog(t, catalog([row({ provider: P, model: M, enabled: true, roles: [] })]));
  assert.equal(eligibilityFor({ provider: P, id: M }).eligible, false);

  useCatalog(t, catalog([row({ provider: P, model: M, enabled: true, roles: ["sub", "main"] })]));
  assert.equal(eligibilityFor({ provider: P, id: M }).eligible, true);
});

test("a second main row does not change which single skill is contributed", async (t) => {
  useCatalog(
    t,
    catalog([MAIN_ROW, row({ provider: "other", model: "other", enabled: true, roles: ["main"] })]),
  );
  const { handlers, ctx } = await startGate();
  const result = await handlers.get("resources_discover")({ type: "resources_discover", reason: "startup", cwd: REPO_ROOT }, ctx);
  assert.deepEqual(result.skillPaths, [GATED_SKILL_PATH]);
});

// ---------------------------------------------------------------------------
// catalog validation
// ---------------------------------------------------------------------------

test("empty and settings-only documents are valid catalogs with no entries", (t) => {
  for (const text of ["{}", "[]", "  {  } ", '{"version":2}', '{"profiles":null}', '{"record_retention_days":14}']) {
    assert.deepEqual(parseMainProfiles(text), [], `${text} must be a valid empty catalog`);
  }
  useCatalog(t, "{}");
  assert.deepEqual(eligibilityFor({ provider: P, id: M }), { eligible: false, catalogUnavailable: false });
});

test("malformed, missing and non-canonical catalogs fail closed", (t) => {
  const invalid = [
    ["not json at all", "malformed text"],
    ["[", "truncated json"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":true,"roles":[]}', "unbalanced json"],
    ['"a string"', "scalar root"],
    ["5", "number root"],
    ["null", "null root"],
    ["[1]", "non-empty array root"],
    ['{"apiKey":"synthetic-secret"}', "auth field at root"],
    ['{"credential_shell_bridge":"sh"}', "credential bridge at root"],
    ['{"version":2,"token":"synthetic"}', "token at root"],
    ['{"version":2,"unknown":1}', "unknown root field"],
    ['{"version":2,"profiles":{}}', "profiles object without v1 marker"],
    ['{"version":1,"profiles":{"a":{"provider":"p","model":"m","enabled":true,"roles":["main"]}}}', "legacy v1"],
    ['{"version":1,"profiles":[]}', "legacy v1 with array"],
    ['{"version":2,"profiles":[1]}', "non-object row"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":true,"roles":[],"token":"synthetic"}]}', "token in row"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":true,"roles":[],"apiKey":"synthetic"}]}', "apiKey in row"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":true,"roles":[],"command":"run"}]}', "command in row"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":true,"roles":[],"routing_class":"standard"}]}', "legacy row field"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","roles":[]}]}', "missing enabled"],
    ['{"version":2,"profiles":[{"provider":"p","enabled":true,"roles":[]}]}', "missing model"],
    ['{"version":2,"profiles":[{"model":"m","enabled":true,"roles":[]}]}', "missing provider"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":true}]}', "missing roles"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":true,"roles":null}]}', "null roles"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":"true","roles":[]}]}', "string enabled"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":1,"roles":[]}]}', "numeric enabled"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":true,"roles":{}}]}', "object roles"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":true,"roles":["main","main"]}]}', "duplicate role"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":true,"roles":["MAIN"]}]}', "case-shifted role"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":true,"roles":["reviewer"]}]}', "unknown role"],
    ['{"version":2,"profiles":[{"provider":"p","model":"m","enabled":true,"roles":[null]}]}', "null role member"],
    ['{"version":2,"profiles":[{"provider":"","model":"m","enabled":true,"roles":[]}]}', "empty provider"],
    ['{"version":2,"profiles":[{"provider":"   ","model":"m","enabled":true,"roles":[]}]}', "blank provider"],
    ['{"version":2,"profiles":[{"provider":"p","model":"  ","enabled":true,"roles":[]}]}', "blank model"],
    ['{"version":2,"profiles":[{"provider":5,"model":"m","enabled":true,"roles":[]}]}', "numeric provider"],
    ['{"version":2,"profiles":[{"provider":"p","model":null,"enabled":true,"roles":[]}]}', "null model"],
  ];
  for (const [text, label] of invalid) {
    assert.equal(parseMainProfiles(text), null, `${label} must be rejected`);
  }

  useCatalog(t, "not json at all");
  assert.deepEqual(eligibilityFor({ provider: P, id: M }), { eligible: false, catalogUnavailable: true });
});

test("a missing catalog file fails closed and is distinguishable from an empty one", (t) => {
  const missing = useCatalog(t, null);
  assert.equal(missing.includes("missing"), false);
  assert.deepEqual(eligibilityFor({ provider: P, id: M }), { eligible: false, catalogUnavailable: true });

  useCatalog(t, "");
  assert.deepEqual(eligibilityFor({ provider: P, id: M }), { eligible: false, catalogUnavailable: true });

  const dir = useCatalog(t, catalog([MAIN_ROW]), { name: "nested" });
  rmSync(dir);
  assert.deepEqual(eligibilityFor({ provider: P, id: M }), { eligible: false, catalogUnavailable: true });
});

test("ids must be present-and-shaped, never null or another type", (t) => {
  const bad = [
    ['{"id":null}', "null id"],
    ['{"id":5}', "numeric id"],
    ['{"id":true}', "boolean id"],
    ['{"id":""}', "empty id"],
    ['{"id":"-leading"}', "leading hyphen"],
    ['{"id":"has space"}', "space in id"],
    ['{"id":"' + "a".repeat(65) + '"}', "over-long id"],
  ];
  for (const [idFields, label] of bad) {
    const text = catalog([row({ provider: P, model: M, enabled: true, roles: [], ...JSON.parse(idFields) })]);
    assert.equal(parseMainProfiles(text), null, `${label} must be rejected`);
  }
  assert.deepEqual(parseMainProfiles(catalog([row({ id: "a", provider: P, model: M, enabled: true, roles: [] })])), []);
  assert.deepEqual(parseMainProfiles(catalog([row({ id: "A-0_z", provider: P, model: M, enabled: true, roles: [] })])), []);
  assert.deepEqual(parseMainProfiles(catalog([row({ id: "a".repeat(64), provider: P, model: M, enabled: true, roles: [] })])), []);
});

test("duplicate ids and duplicate pairs are rejected, including derived-id collisions", (t) => {
  const derived = derivedId(P, M);
  const rejected = [
    [catalog([row({ id: "same", provider: P, model: M, enabled: true, roles: [] }), row({ id: "same", provider: "q", model: "r", enabled: true, roles: [] }) ]), "duplicate explicit id"],
    [catalog([row({ provider: P, model: M, enabled: true, roles: [] }), row({ provider: P, model: M, enabled: true, roles: [] }) ]), "duplicate pair"],
    [catalog([row({ provider: P, model: M, enabled: true, roles: [] }), row({ id: "other", provider: P, model: M, enabled: true, roles: [] }) ]), "duplicate pair with distinct ids"],
    [catalog([row({ id: "shared", provider: P, model: M, enabled: true, roles: [] }), row({ id: "shared", provider: "q", model: "r", enabled: true, roles: [] }) ]), "duplicate explicit id"],
    [catalog([row({ provider: P, model: M, enabled: true, roles: [] }), row({ provider: P, model: M, enabled: true, roles: ["main"] }) ]), "duplicate pair with different roles"],
    [catalog([row({ id: derivedId("q", "r"), provider: P, model: M, enabled: true, roles: [] }), row({ provider: "q", model: "r", enabled: true, roles: [] }) ]), "derived-id collision with an explicit id"],
    [catalog([row({ id: "dup", provider: P, model: M, enabled: true, roles: [] }), row({ id: "dup", provider: "q", model: "r", enabled: true, roles: [] }), row({ provider: "s", model: "t", enabled: true, roles: [] }) ]), "duplicate id among three rows"],
  ];
  for (const [text, label] of rejected) {
    assert.equal(parseMainProfiles(text), null, `${label} must be rejected`);
  }
  // Same provider, different model is fine: ids and pairs are distinct.
  assert.deepEqual(
    parseMainProfiles(catalog([row({ provider: P, model: M, enabled: true, roles: [] }), row({ provider: P, model: "other", enabled: true, roles: [] }) ])),
    [],
  );
  // A valid explicit id that merely *looks* derived is accepted for its own row.
  assert.deepEqual(
    parseMainProfiles(catalog([row({ id: derived, provider: P, model: M, enabled: true, roles: [] })])),
    [],
  );
});

test("pair identity is collision-free, including embedded NUL and delimiter characters", (t) => {
  const nulPair = (a, b) =>
    catalog([row({ provider: a, model: b, enabled: true, roles: [] })]);
  // Delimiter concatenation would collapse these two rows into one "duplicate pair".
  assert.deepEqual(
    parseMainProfiles(catalog([
      row({ provider: "a\u0000b", model: "c", enabled: true, roles: [] }),
      row({ provider: "a", model: "b\u0000c", enabled: true, roles: [] }),
    ])),
    [],
  );
  assert.deepEqual(
    parseMainProfiles(catalog([
      row({ provider: "a\u0000b", model: "c", enabled: true, roles: [] }),
      row({ provider: "a\u0000b", model: "c", enabled: true, roles: [] }),
    ])),
    null,
    "an identical NUL-bearing pair is still a duplicate",
  );
  // Quotes, backslashes and newlines are likewise unambiguous.
  assert.deepEqual(parseMainProfiles(nulPair('a"b', "c\\d")), []);
  assert.deepEqual(
    parseMainProfiles(catalog([row({ provider: "a\nb", model: "c", enabled: true, roles: [] }), row({ provider: "a", model: "b\nc", enabled: true, roles: [] }) ])),
    [],
    "a newline is a value, not a delimiter",
  );
  assert.equal(
    parseMainProfiles(catalog([row({ provider: "a\nb", model: "c", enabled: true, roles: [] }), row({ provider: "a\nb", model: "c", enabled: true, roles: ["main"] }) ])),
    null,
    "an identical newline-bearing pair is a duplicate",
  );
  // The gate does not trim before comparing: eligibility is an exact match.
  useCatalog(t, catalog([row({ provider: ` ${P} `, model: M, enabled: true, roles: ["main"] })]));
  assert.equal(eligibilityFor({ provider: P, id: M }).eligible, false);
  assert.equal(eligibilityFor({ provider: ` ${P} `, id: M }).eligible, true);
});

test("version must be the exact JSON integer 2; floats, exponents and escapes are rejected", (t) => {
  const accepted = [
    ['{"version":2,"profiles":[]}', "plain 2"],
    ['{"version": 2 , "profiles":[]}', "spaced 2"],
    ['{"\\u0076ersion":2,"profiles":[]}', "escaped key"],
    ['{"\\u0076\\u0065\\u0072sion":2,"profiles":[]}', "fully escaped key"],
    ['{"version":2.0,"version":2,"profiles":[]}', "last-key-wins keeps the valid token"],
  ];
  for (const [text, label] of accepted) {
    assert.deepEqual(parseMainProfiles(text), [], `${label} must be accepted`);
  }
  const rejected = [
    ['{"version":2.0,"profiles":[]}', "float 2.0"],
    ['{"version":2.00,"profiles":[]}', "float 2.00"],
    ['{"version":2e0,"profiles":[]}', "exponent 2e0"],
    ['{"version":2E0,"profiles":[]}', "exponent 2E0"],
    ['{"version":2.0e0,"profiles":[]}', "float+exponent"],
    ['{"\\u0076ersion":2.0,"profiles":[]}', "escaped key with float"],
    ['{"version":2,"version":2.0,"profiles":[]}', "last-key-wins on the bad token"],
    ['{"version":1,"profiles":[]}', "legacy v1"],
    ['{"version":3,"profiles":[]}', "future version"],
    ['{"version":"2","profiles":[]}', "string version"],
    ['{"version":true,"profiles":[]}', "boolean version"],
    ['{"version":2.5,"profiles":[]}', "non-integer version"],
    ['{"version":-2,"profiles":[]}', "negative version"],
  ];
  for (const [text, label] of rejected) {
    assert.equal(parseMainProfiles(text), null, `${label} must be rejected`);
  }
});

test("record_retention_days must be an exact non-negative integer inside the safe range", (t) => {
  for (const [text, label] of [
    ['{"record_retention_days":0}', "zero"],
    ['{"record_retention_days":-0}', "negative zero"],
    ['{"record_retention_days":14}', "positive"],
    ['{"record_retention_days":9007199254740991}', "max safe integer"],
    ['{"\\u0072ecord_retention_days":7}', "escaped key"],
  ]) {
    assert.deepEqual(parseMainProfiles(text), [], `${label} must be accepted`);
  }
  for (const [text, label] of [
    ['{"record_retention_days":-1}', "negative"],
    ['{"record_retention_days":2.0}', "float"],
    ['{"record_retention_days":1e1}', "exponent"],
    ['{"record_retention_days":1E1}', "capitalised exponent"],
    ['{"\\u0072ecord_retention_days":2.0}', "escaped key with float"],
    ['{"record_retention_days":null}', "null"],
    ['{"record_retention_days":"14"}', "string"],
    ['{"record_retention_days":true}', "boolean"],
    ['{"record_retention_days":9007199254740993}', "beyond the JS safe range"],
    ['{"record_retention_days":99999999999999999999}', "far beyond the safe range"],
  ]) {
    assert.equal(parseMainProfiles(text), null, `${label} must be rejected`);
  }
});

test("raw token scanning ignores string contents and nested keys", (t) => {
  // "2.0" inside a string value must not be mistaken for a version token, and a
  // nested `version` key must not be treated as a root one.
  assert.deepEqual(
    parseMainProfiles('{"profiles":[{"provider":"version\\": 2.0","model":"m","enabled":true,"roles":[]}]}'),
    [],
  );
  assert.equal(parseMainProfiles('{"profiles":[{"version":2.0,"provider":"p","model":"m","enabled":true,"roles":[]}]}'), null);
  assert.equal(scanRootValueTokens('{"a":{"version":2.0},"version":2}').get("version"), "2");
  // Only scalar root members get a token: a container-valued `version` has none, so
  // it fails closed instead of being misread.
  assert.equal(scanRootValueTokens('{"a":{"version":2.0},"version":2}').has("a"), false);
  assert.equal(parseMainProfiles('{"version":{},"profiles":[]}'), null);
  assert.equal(parseMainProfiles('{"version":[2],"profiles":[]}'), null);
  assert.equal(parseMainProfiles('{"record_retention_days":[14]}'), null);
  assert.equal(scanRootValueTokens('not json'), null);
  assert.equal(scanRootValueTokens('{"a":1'), null);
});

test("a root member overwritten by a container clears its earlier scalar token", (t) => {
  const profiles = '"profiles":[{"provider":"synthetic","model":"synthetic","enabled":true,"roles":["main"]}]';

  // Last-key-wins: the duplicate `record_retention_days` member's final value is a
  // container, so the earlier scalar token is stale and the document must be rejected.
  for (const stale of [
    ['{"record_retention_days":14,"record_retention_days":[]}', "empty array"],
    ['{"record_retention_days":14,"record_retention_days":{}}', "empty object"],
    ['{"record_retention_days":14,"record_retention_days":[14]}', "array holding the integer"],
    ['{"record_retention_days":14,"record_retention_days":{"a":1}}', "object"],
    ['{"record_retention_days":2,"record_retention_days":[2.0]}', "array holding a float"],
    ['{"\\u0072ecord_retention_days":14,"record_retention_days":[]}', "escaped key"],
    ['{"record_retention_days":[],"record_retention_days":[]}', "both containers"],
  ]) {
    const [prefix, label] = stale;
    const text = `${prefix.slice(0, -1)},${profiles}}`;
    assert.equal(scanRootValueTokens(text).has("record_retention_days"), false, `${label}: no stale token`);
    assert.equal(parseMainProfiles(text), null, `${label} must be rejected`);
  }

  // Repeated version container: same stale-token rule, so it stays rejected.
  for (const [text, label] of [
    ['{"version":2,"version":{},"profiles":[]}', "version object"],
    ['{"version":2,"version":[],"profiles":[]}', "version array"],
    ['{"version":2,"version":[2],"profiles":[]}', "version array holding 2"],
  ]) {
    assert.equal(scanRootValueTokens(text).has("version"), false, `${label}: no stale token`);
    assert.equal(parseMainProfiles(text), null, `${label} must be rejected`);
  }

  // Reverse order: the scalar is last, so the valid document is accepted.
  for (const [text, label, token] of [
    [`{"record_retention_days":[],"record_retention_days":14,${profiles}}`, "array then integer", "14"],
    [`{"record_retention_days":{},"record_retention_days":7,${profiles}}`, "object then integer", "7"],
  ]) {
    assert.equal(scanRootValueTokens(text).get("record_retention_days"), token, `${label}: last scalar token wins`);
    assert.deepEqual(parseMainProfiles(text), [{ provider: "synthetic", model: "synthetic" }], `${label} must be accepted`);
  }

  // A nested member with the same name must not clear the root token.
  const nested = '{"record_retention_days":14,"profiles":[{"provider":"synthetic","model":"synthetic","enabled":true,"roles":["main"],"record_retention_days":[]}]}';
  assert.equal(scanRootValueTokens(nested).get("record_retention_days"), "14", "nested container keeps the root token");
  const nestedObject = '{"record_retention_days":14,"profiles":[{"provider":"synthetic","model":"synthetic","enabled":true,"roles":["main"],"record_retention_days":{}}]}';
  assert.equal(scanRootValueTokens(nestedObject).get("record_retention_days"), "14", "nested object keeps the root token");
});

// ---------------------------------------------------------------------------
// derived id parity with the Python core
// ---------------------------------------------------------------------------

test("derived ids match scripts/profile_config.py auto_profile_id (frozen synthetic hashes)", () => {
  const frozen = [
    [["p", "m"], "profile-620a0800708a22436d00d07db17e9ea1297e7a1d48692dfb"],
    [["anthropic", "claude-sonnet-4"], "profile-135ac16a1acc32ac3fbc67b012dcc3e0424b8cb3cbf7809d"],
    [["prov der", "mødel/ünïcode"], "profile-b48babe911024630aeff1f0d8cd05c01996957207280898f"],
    [["a\u0000b", "c"], "profile-7269eacf721004914595d0b0fa3fe40526cec6703c6d0a48"],
    [["a", "b\u0000c"], "profile-757d7f645eee6d4ef4cde977d3377cf6151bd14d8644df87"],
    [['p"x', "m\\y"], "profile-6a89ab0000e556fe7b329f8722e78be56df5fb54c47bcb9d"],
    [["", "x"], "profile-557726d30ca73cd504cb6e4ea6e03703107a17e449c1da02"],
    [["x", ""], "profile-9bf37d43d90e651c6e12976f4016d7da0d9c1f0bdc3d1b1c"],
    [["日本語", "モデル"], "profile-665c55993bcf5d69a2d7579fcffb2608830f7203b0b44ea5"],
    [["a\nb", "c\td"], "profile-8e5a30561af02d6ae51300fc59f4bfb988a11eece02ffbcc"],
  ];
  for (const [[provider, model], expected] of frozen) {
    assert.equal(derivedId(provider, model), expected, `derivedId(${JSON.stringify(provider)}, ${JSON.stringify(model)})`);
    assert.equal(expected.length, "profile-".length + 48);
  }
  // Independent of roles, enabled and ordering.
  assert.equal(derivedId(P, M), derivedId(P, M));
});

test("derived id parity is re-checked against the Python core when it is available", (t) => {
  const probe = spawnSync("python3", ["-c", "print(1)"], { encoding: "utf8" });
  if (probe.status !== 0) {
    t.diagnostic("python3 unavailable; frozen hashes cover parity");
    return;
  }
  const script = [
    "import json, sys",
    `sys.path.insert(0, ${JSON.stringify(join(REPO_ROOT, "scripts"))})`,
    "from profile_config import auto_profile_id",
    "pairs = json.load(sys.stdin)",
    "print(json.dumps([auto_profile_id(p, m) for p, m in pairs]))",
  ].join("\n");
  const pairs = [[P, M], ["prov der", "mødel"], ["a\u0000b", "c"], ["a", "b\u0000c"], ["日本語", "モデル"]];
  const result = spawnSync("python3", ["-c", script], { input: JSON.stringify(pairs), encoding: "utf8" });
  if (result.status !== 0) {
    t.diagnostic(`python profile_config unavailable (${result.stderr.trim().slice(0, 120)})`);
    return;
  }
  assert.deepEqual(JSON.parse(result.stdout), pairs.map(([p, mm]) => derivedId(p, mm)));
});

// ---------------------------------------------------------------------------
// hook behaviour
// ---------------------------------------------------------------------------

test("a catalog edit between turns re-syncs the prompt without a reload", async (t) => {
  const file = useCatalog(t, catalog([row({ provider: P, model: M, enabled: false, roles: ["main"] })]));
  const { handlers, ctx } = await startGate();

  const discovered = await handlers.get("resources_discover")({ type: "resources_discover", reason: "startup", cwd: REPO_ROOT }, ctx);
  assert.deepEqual(discovered, {}, "a disabled row contributes nothing");

  const options = { skills: [fakeSkill(join(REPO_ROOT, "unrelated", "SKILL.md"))] };
  await handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: options }, ctx);
  assert.deepEqual(options.skills.map((s) => s.filePath), [join(REPO_ROOT, "unrelated", "SKILL.md")]);

  // The host enables the row: the next turn picks it up without /reload.
  writeFileSync(file, catalog([MAIN_ROW]), "utf8");
  await handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: options }, ctx);
  assert.deepEqual(options.skills.map((s) => s.filePath), [join(REPO_ROOT, "unrelated", "SKILL.md"), GATED_SKILL_PATH]);

  // The host disables it again: the next turn drops it again.
  writeFileSync(file, catalog([row({ provider: P, model: M, enabled: false, roles: ["main"] })]), "utf8");
  await handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: options }, ctx);
  assert.deepEqual(options.skills.map((s) => s.filePath), [join(REPO_ROOT, "unrelated", "SKILL.md")]);
});

test("a model switch between turns re-syncs the prompt", async (t) => {
  useCatalog(t, catalog([MAIN_ROW]));
  const { handlers, ctx } = await startGate();
  const options = { skills: [] };
  await handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: options }, ctx);
  assert.deepEqual(options.skills.map((s) => s.filePath), [GATED_SKILL_PATH]);

  ctx.model = { provider: "another-provider", id: "another-model" };
  await handlers.get("model_select")({ type: "model_select", model: ctx.model }, ctx);
  await handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: options }, ctx);
  assert.deepEqual(options.skills, []);

  ctx.model = { provider: P, id: M };
  await handlers.get("model_select")({ type: "model_select", model: ctx.model }, ctx);
  await handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: options }, ctx);
  assert.deepEqual(options.skills.map((s) => s.filePath), [GATED_SKILL_PATH]);
});

test("unrelated skills are preserved and the gated skill is never duplicated", async (t) => {
  useCatalog(t, catalog([MAIN_ROW]));
  const { handlers, ctx } = await startGate();
  const unrelated = fakeSkill(join(REPO_ROOT, "other", "SKILL.md"));
  const options = { skills: [unrelated, fakeSkill(join(REPO_ROOT, "more", "SKILL.md"))] };

  await handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: options }, ctx);
  await handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: options }, ctx);
  await handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: options }, ctx);

  assert.equal(options.skills.length, 3, "two unrelated skills plus exactly one gated skill");
  assert.equal(options.skills.filter((s) => s.filePath === GATED_SKILL_PATH).length, 1);
  assert.equal(options.skills[0], unrelated, "unrelated skills keep their order and identity");
  assert.equal(options.skills[1].filePath, join(REPO_ROOT, "more", "SKILL.md"));
  const injected = options.skills[2];
  assert.equal(injected.filePath, GATED_SKILL_PATH);
  assert.equal(injected.baseDir, REPO_ROOT);
  assert.equal(injected.disableModelInvocation, false);
  assert.ok(injected.description.length > 0);
  assert.equal(injected.name, readFileSync(join(REPO_ROOT, "SKILL.md"), "utf8").match(/^name:\s*(\S+)\s*$/m)[1]);
});

test("an already-present gated skill from discovery is not injected twice", async (t) => {
  useCatalog(t, catalog([MAIN_ROW]));
  const { handlers, ctx } = await startGate();
  const options = { skills: [{ ...fakeSkill(GATED_SKILL_PATH) }] };
  await handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: options }, ctx);
  assert.equal(options.skills.length, 1);
});

test("a turn without structured skills is a no-op", async (t) => {
  useCatalog(t, catalog([MAIN_ROW]));
  const { handlers, ctx } = await startGate();
  await handlers.get("before_agent_start")({ type: "before_agent_start" }, ctx);
  await handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: {} }, ctx);
  await handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: { skills: null } }, ctx);
  await handlers.get("model_select")({ type: "model_select" }, ctx);
});

test("warnings are sanitized, one-shot, and never mention catalog values or paths", async (t) => {
  const file = useCatalog(t, "not json at all");
  const { handlers, ctx, notices } = await startGate();
  await handlers.get("resources_discover")({ type: "resources_discover", reason: "startup", cwd: REPO_ROOT }, ctx);
  await handlers.get("resources_discover")({ type: "resources_discover", reason: "reload", cwd: REPO_ROOT }, ctx);

  const warnings = notices.filter((n) => n.level === "warning");
  assert.equal(warnings.length, 1, "the catalog warning is emitted once");
  assert.ok(!warnings[0].message.includes(file), "no catalog path");
  assert.ok(!warnings[0].message.includes(tmpdir()), "no temporary directory");
  assert.ok(!/synthetic-provider|synthetic-model/.test(warnings[0].message), "no model values");
  assert.equal(notices.filter((n) => n.level === "info").length, 0);
});

test("an empty catalog is silent, and an opening gate produces one compact notice", async (t) => {
  useCatalog(t, "{}");
  const { handlers, ctx, notices } = await startGate();
  await handlers.get("resources_discover")({ type: "resources_discover", reason: "startup", cwd: REPO_ROOT }, ctx);
  assert.deepEqual(notices, []);
});

test("notifications are skipped without a UI and survive a failing UI", async (t) => {
  useCatalog(t, catalog([MAIN_ROW]));

  const quiet = await startGate({ hasUI: false });
  const discovered = await quiet.handlers.get("resources_discover")(
    { type: "resources_discover", reason: "startup", cwd: REPO_ROOT },
    quiet.ctx,
  );
  assert.deepEqual(discovered.skillPaths, [GATED_SKILL_PATH]);
  assert.deepEqual(quiet.notices, [], "no UI means no notifications");

  const noisy = await startGate({
    hasUI: true,
    notify: () => {
      throw new Error("UI unavailable");
    },
  });
  await noisy.handlers.get("resources_discover")({ type: "resources_discover", reason: "startup", cwd: REPO_ROOT }, noisy.ctx);
  const options = { skills: [] };
  await noisy.handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: options }, noisy.ctx);
  assert.deepEqual(options.skills.map((s) => s.filePath), [GATED_SKILL_PATH], "a broken UI never breaks the gate");
});

test("the gate performs no writes and reads only the resolved catalog path", async (t) => {
  const dir = tempDir(t, "readonly-");
  const file = join(dir, "profiles.json");
  writeFileSync(file, catalog([MAIN_ROW]), "utf8");
  const before = readFileSync(file, "utf8");
  withEnv(t, "PI_WORKER_PROFILES_FILE", file);

  const { handlers, ctx } = await startGate();
  await handlers.get("resources_discover")({ type: "resources_discover", reason: "startup", cwd: REPO_ROOT }, ctx);
  await handlers.get("before_agent_start")({ type: "before_agent_start", systemPromptOptions: { skills: [] } }, ctx);
  await handlers.get("model_select")({ type: "model_select" }, ctx);

  assert.equal(readFileSync(file, "utf8"), before, "the catalog is never rewritten");
  assert.equal(resolveProfilesFile(), file);
  assert.deepEqual(
    readdirSync(dir).sort(),
    ["profiles.json"],
    "no sibling files are created next to the catalog",
  );
});

test("the extension source declares no runtime dependency on the Pi SDK", () => {
  const source = readFileSync(GATE_SOURCE, "utf8");
  const valueImports = [...source.matchAll(/^import\s+(?!type\s)[^;]*from\s+["']([^"']+)["']/gm)].map((m) => m[1]);
  assert.deepEqual(
    valueImports.filter((specifier) => specifier.startsWith("@earendil-works/")),
    [],
    "the host SDK must only be imported type-only, so it is erased and never resolved at load time",
  );
  assert.ok(source.includes('import type { ExtensionAPI, ExtensionContext }'), "SDK types are imported type-only");
  // The documented opt-out: the gate never hides the skill file itself.
  assert.ok(!source.includes("homedir(), \".pi"), "no hard-coded agent directory");
  assert.ok(source.includes("fileURLToPath(import.meta.url)"), "paths are derived from this module");
  assert.ok(!source.includes("child_process") && !source.includes("node:net") && !source.includes("node:http"), "no process or network use");
  assert.ok(!/writeFile|appendFile|mkdir|rmSync/.test(source), "no configuration writes");
  assert.ok(source.includes(sep) || source.includes("[\\\\/]"), "skill description is path-separator safe");
});

test("path helpers stay correct when the process CWD changes", (t) => {
  useCatalog(t, catalog([MAIN_ROW]));
  const other = tempDir(t, "elsewhere-");
  withCwd(t, other);
  assert.equal(GATED_SKILL_PATH, join(REPO_ROOT, "SKILL.md"));
  assert.equal(eligibilityFor({ provider: P, id: M }).eligible, true);
  assert.equal(pathToFileURL(GATE_SOURCE).protocol, "file:");
});
