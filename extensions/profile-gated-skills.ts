/**
 * Portable model-gated skills for the public pi-worker Pi package.
 *
 * The package manifest declares `pi.extensions = ["extensions/profile-gated-skills.ts"]`
 * and `pi.skills = []`, so the bundled root `SKILL.md` is contributed *only* through
 * this gate. The skill path is resolved from this module's own location
 * (`<package>/extensions/../SKILL.md`), never from a hard-coded home, repository or
 * working-directory layout, so a relocated copy (including one under a directory with
 * spaces) always gates its own `SKILL.md`.
 *
 * Eligibility is read from the single canonical private roles catalog
 * (`PI_WORKER_PROFILES_FILE`, else `~/.config/pi-worker/profiles.json`) — the same file
 * the Python runner uses, validated against the canonical schema version 2 roles
 * document of `scripts/profile_config.py`. A row unlocks the skill only when
 * `enabled === true`, its unique `roles` list contains `"main"`, and its
 * `provider`/`model` exactly equal the active `ctx.model.provider`/`ctx.model.id`.
 * There is no name matching, no wildcard, no provider-agnostic fallback and no
 * built-in model list: the catalog is the only source of truth, so nothing here ever
 * selects, switches or registers a model, and no main-model default is introduced.
 *
 * The gate fails closed. A catalog that is missing, unreadable, not JSON, legacy
 * version 1, or in any way non-canonical contributes no main-eligible rows and hides
 * the skill. `{}`, `[]`, an absent/`null` `profiles`, and rows with empty or
 * `sub`-only roles are valid *empty* catalogs (no entries) and are not warnings.
 *
 * Scope and honest limits:
 * - This is **discovery gating**, not a security sandbox or a permission boundary.
 *   It cannot retract instructions the model already read, nor erase conversation
 *   history, and it is trivially bypassable by loading the same `SKILL.md` directly
 *   (`--skill`, `settings.skills`, or a copy under a scanned skills directory), which
 *   remains an explicit, human opt-out rather than an automatic behaviour.
 * - Loading standalone (ungated) is always possible and intentional; the gate only
 *   governs what the package advertises by default.
 * - No process spawn, no network, no credential handling and no configuration writes:
 *   the catalog is read once per hook with a plain `readFileSync` of the trusted
 *   environment variable or the user's home directory. The repository and the
 *   working directory are never searched, so a `profiles.json` next to the project
 *   is ignored by design. Raw file contents, file paths, catalog values and model ids
 *   are never logged.
 * - Because Pi resolves relative paths against the process CWD, a *relative*
 *   `PI_WORKER_PROFILES_FILE` is interpreted against the CWD the host was started in;
 *   use an absolute path to avoid ambiguity.
 * - At startup and on `/reload` the `resources_discover` hook contributes the path
 *   through Pi's official loading path. Mid-session model switches and catalog edits
 *   are handled by `before_agent_start`, which re-syncs
 *   `event.systemPromptOptions.skills` on every turn: Pi diffs the resulting prompt
 *   sections and appends a single patch message with only the changed sections, so no
 *   `/reload` is needed for prompt availability. The `/skill:<name>` command,
 *   however, is registered only during resource discovery, so run `/reload` to
 *   register or unregister it.
 */

import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { Skill } from "@earendil-works/pi-coding-agent/dist/core/skills";

/** Directory holding this extension file, always derived from this module's own URL. */
export const EXTENSION_DIR = dirname(fileURLToPath(import.meta.url));

/**
 * The single gated skill: the package root `SKILL.md`, i.e. the parent directory of
 * `extensions/`. Resolution is relative to this file, so a copy of the package under
 * any directory name gates that copy's own `SKILL.md`.
 */
export const GATED_SKILL_PATH = resolve(EXTENSION_DIR, "..", "SKILL.md");

/** Environment variable that relocates the canonical private roles catalog. */
export const PROFILES_FILE_ENV = "PI_WORKER_PROFILES_FILE";

/** Canonical default location of the roles catalog (home directory only). */
export function defaultProfilesFile(): string {
  return join(homedir(), ".config", "pi-worker", "profiles.json");
}

/** Roles the catalog understands. Only `main` unlocks the gated skill. */
export const MAIN_ROLE = "main";
const KNOWN_ROLES: ReadonlySet<string> = new Set([MAIN_ROLE, "sub"]);

/** Only these root keys are accepted; anything else (including auth-ish keys) rejects the catalog. */
const ROOT_FIELDS: ReadonlySet<string> = new Set(["profiles", "version", "record_retention_days"]);

/**
 * Only these per-row keys are accepted. Credential/token/key/shell/command style keys
 * are therefore rejected as unknown fields — this gate never accepts secret values or
 * user commands in configuration (auth is solely Pi's own default).
 */
const ROW_FIELDS: ReadonlySet<string> = new Set(["id", "provider", "model", "enabled", "roles"]);

/** Bounded ASCII profile id, the same shape the Python loader accepts. */
const PROFILE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

/** Canonical schema version this gate speaks. Legacy version 1 is rejected. */
const CANONICAL_VERSION = 2;

/** Largest magnitude accepted for a JSON integer token (JavaScript safe-integer range). */
const MAX_SAFE_BIGINT = BigInt(Number.MAX_SAFE_INTEGER);

const JSON_WHITESPACE = new Set([" ", "\t", "\n", "\r"]);

export interface MainProfileRow {
  provider: string;
  model: string;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Resolve the catalog path from the trusted environment variable or the user's home
 * directory. The repository, the Pi package location and the working directory are
 * never consulted.
 */
export function resolveProfilesFile(): string {
  const fromEnv = process.env[PROFILES_FILE_ENV];
  if (typeof fromEnv === "string" && fromEnv.trim()) return fromEnv.trim();
  return defaultProfilesFile();
}

/**
 * Stable derived id for a row without an explicit `id`, byte-identical to the Python
 * core `profile_config.auto_profile_id`: `sha256` over the canonical JSON object
 * `{"model":…,"provider":…}` (compact separators, non-ASCII left unescaped) encoded as
 * UTF-8, hex-truncated to 48 characters and prefixed with `profile-`.
 *
 * The keys are emitted in sorted order (`model` before `provider`) to match Python's
 * `sort_keys=True`, so both runtimes produce the same digest for the same pair. The
 * value is independent of `enabled`, `roles` and array order, and is used only for
 * id-collision detection: an automatically derived id that equals another row's id
 * fails the catalog, exactly as the Python loader does.
 */
export function derivedId(provider: string, model: string): string {
  const canonical = JSON.stringify({ model, provider });
  return `profile-${createHash("sha256").update(canonical, "utf8").digest("hex").slice(0, 48)}`;
}

/**
 * Collision-free key for a provider/model pair.
 *
 * A JSON array encoding is injective: every string (including embedded NULs, quotes,
 * backslashes and newlines) is escaped unambiguously, so `("a\0b","c")` and
 * `("a","b\0c")` are distinct. Delimiter concatenation would collide here, so it is
 * deliberately not used.
 */
function pairKey(provider: string, model: string): string {
  return JSON.stringify([provider, model]);
}

interface ScanFrame {
  isObject: boolean;
  expect: "key" | "colon" | "value" | "comma";
  key: string | null;
}

/**
 * Collect the **raw JSON value token** of each member of the root JSON object.
 *
 * `JSON.parse` collapses the token `2.0`/`2e0` to the number `2`, and it also decodes
 * escaped property names, so a text-level regex cannot enforce "exact JSON integer"
 * reliably (it also matches digits inside string values). This scanner therefore
 * tokenises the document itself: string values are consumed with escape handling, so
 * their contents are never mistaken for tokens; only direct members of the root
 * object are recorded, so a nested `version` key is ignored; and a member written more
 * than once is recorded last, matching `JSON.parse`'s last-key-wins semantics.
 *
 * Returns `null` only when the text is not a structurally scannable JSON document
 * (in which case `JSON.parse` below will also fail and the catalog is rejected).
 */
export function scanRootValueTokens(text: string): Map<string, string> | null {
  const members = new Map<string, string>();
  const stack: ScanFrame[] = [];
  const length = text.length;
  let index = 0;

  const inRootObject = (): boolean => stack.length === 1 && stack[0]!.isObject;

  const readString = (): void => {
    const start = index;
    index += 1;
    while (index < length) {
      const char = text[index];
      if (char === "\\") {
        index += 2;
        continue;
      }
      if (char === '"') {
        index += 1;
        try {
          JSON.parse(text.slice(start, index));
        } catch {
          throw new Error("unparsable string token");
        }
        return;
      }
      index += 1;
    }
    throw new Error("unterminated string token");
  };

  try {
    while (index < length) {
      const char = text[index]!;
      if (JSON_WHITESPACE.has(char)) {
        index += 1;
        continue;
      }

      if (char === "{" || char === "[") {
        // A container value completes with its closing bracket, after which the
        // enclosing container expects a comma.
        const parent = stack[stack.length - 1];
        if (parent) parent.expect = "comma";
        // Last-key-wins: when a root member's final value is a container, any earlier
        // scalar token for the same key is stale and must be dropped (containers are
        // never recorded). Only a direct root member does this — a nested container
        // never clears a root token.
        if (parent && parent.isObject && stack.length === 1 && parent.key !== null) members.delete(parent.key);
        stack.push({ isObject: char === "{", expect: char === "{" ? "key" : "value", key: null });
        index += 1;
        continue;
      }
      if (char === "}" || char === "]") {
        if (stack.length === 0) throw new Error("unbalanced container");
        stack.pop();
        index += 1;
        continue;
      }

      const frame = stack[stack.length - 1];
      if (!frame) throw new Error("value outside any container");

      if (frame.isObject && frame.expect === "key") {
        if (char !== '"') throw new Error("expected object key");
        const start = index;
        readString();
        frame.key = JSON.parse(text.slice(start, index)) as string;
        frame.expect = "colon";
        continue;
      }
      if (frame.isObject && frame.expect === "colon") {
        if (char !== ":") throw new Error("expected colon");
        index += 1;
        frame.expect = "value";
        continue;
      }
      if (frame.expect === "comma") {
        if (char !== ",") throw new Error("expected comma");
        index += 1;
        frame.expect = frame.isObject ? "key" : "value";
        continue;
      }

      // frame.expect === "value"
      const rootMember = frame.isObject && inRootObject();
      const key = frame.key;
      if (char === '"') {
        const start = index;
        readString();
        if (rootMember && key !== null) members.set(key, text.slice(start, index));
      } else if (char === "{" || char === "[") {
        // A container value completes with its closing bracket; the parent then expects
        // a comma, so mark it before descending.
        frame.expect = "comma";
        stack.push({ isObject: char === "{", expect: char === "{" ? "key" : "value", key: null });
        index += 1;
        continue;
      } else {
        const start = index;
        while (index < length && !JSON_WHITESPACE.has(text[index]!) && !",}]".includes(text[index]!)) {
          index += 1;
        }
        if (index === start) throw new Error("empty value token");
        if (rootMember && key !== null) members.set(key, text.slice(start, index));
      }
      frame.expect = "comma";
    }
    if (stack.length > 0) throw new Error("unterminated container");
    return members;
  } catch {
    return null;
  }
}

/** True only for an exact, non-negative JSON integer token inside the safe-integer range. */
function isNonNegativeIntegerToken(token: string | undefined): boolean {
  if (typeof token !== "string" || !/^-?\d+$/.test(token)) return false;
  let value: bigint;
  try {
    value = BigInt(token);
  } catch {
    return false;
  }
  return value >= 0n && value <= MAX_SAFE_BIGINT;
}

/**
 * Strictly validate the canonical version-2 roles catalog and return its main-eligible
 * rows.
 *
 * Returns `null` (fail closed) for any violation: a non-object root other than the
 * empty `[]` shorthand, unknown root or row field (including credential, token, key,
 * shell or command fields), a legacy `version: 1` document or a bare profiles object,
 * a present `version` that is not the exact integer token `2`, a missing or non-array
 * `profiles`, a negative or non-integer `record_retention_days` (including a float or
 * exponent literal), a row missing `provider`/`model`/`enabled`/`roles`, an empty or
 * non-string provider or model, a non-boolean `enabled`, roles outside `main`/`sub` or
 * repeated, a present but non-string / `null` / out-of-shape `id`, or duplicate
 * provider/model pairs or ids (explicit or derived). Nothing from the file is echoed
 * anywhere.
 *
 * A valid catalog with no eligible row returns `[]` (no entries, no warning), which is
 * how `{}`, `[]`, an absent or `null` `profiles`, `roles: []` and `sub`-only rows are
 * represented.
 */
export function parseMainProfiles(text: string): MainProfileRow[] | null {
  const tokens = scanRootValueTokens(text);
  if (!tokens) return null;

  let document: unknown;
  try {
    document = JSON.parse(text);
  } catch {
    return null;
  }

  // Documented unconfigured shorthand: a bare empty array means "no profiles".
  if (Array.isArray(document)) return document.length === 0 ? [] : null;
  if (!isPlainObject(document)) return null;
  // Settings-only shorthand: an empty object means "no profiles, no models".
  if (Object.keys(document).length === 0) return [];

  for (const key of Object.keys(document)) {
    if (!ROOT_FIELDS.has(key)) return null;
  }

  const hasVersion = "version" in document;
  if (hasVersion) {
    // Exact JSON integer token: `2` only. Rejects `2.0`, `2e0`, `2E0`, `"2"`, `true`
    // and any other version, and thereby rejects the legacy version-1 document. The
    // token is the last occurrence, matching JSON.parse's last-key-wins behaviour.
    if (tokens.get("version") !== String(CANONICAL_VERSION)) return null;
    if (document.version !== CANONICAL_VERSION) return null;
  } else if (isPlainObject(document.profiles)) {
    // A profiles OBJECT is the legacy shape and is invalid without its version-1
    // marker, which this gate rejects anyway.
    return null;
  }

  if ("record_retention_days" in document) {
    // The token enforces the exact literal; the parsed value is checked as defence in
    // depth, so a container (or any non-number) root value can never slip through.
    if (!isNonNegativeIntegerToken(tokens.get("record_retention_days"))) return null;
    const retention: unknown = document.record_retention_days;
    if (typeof retention !== "number" || !Number.isSafeInteger(retention) || retention < 0) return null;
  }

  const rows = document.profiles;
  if (rows === undefined || rows === null) return [];
  if (!Array.isArray(rows)) return null;

  const main: MainProfileRow[] = [];
  const seenIds = new Set<string>();
  const seenPairs = new Set<string>();
  for (const raw of rows) {
    if (!isPlainObject(raw)) return null;
    for (const key of Object.keys(raw)) {
      if (!ROW_FIELDS.has(key)) return null;
    }
    for (const required of ["provider", "model", "enabled", "roles"]) {
      if (!(required in raw)) return null;
    }

    const provider = raw.provider;
    const model = raw.model;
    if (typeof provider !== "string" || !provider.trim()) return null;
    if (typeof model !== "string" || !model.trim()) return null;
    if (typeof raw.enabled !== "boolean") return null;

    const roles: string[] = [];
    if (!Array.isArray(raw.roles)) return null;
    for (const role of raw.roles) {
      if (typeof role !== "string" || !KNOWN_ROLES.has(role)) return null;
      if (roles.includes(role)) return null;
      roles.push(role);
    }

    // A present `id` must be a string in shape; `null` is rejected rather than being
    // silently replaced by the derived id, matching the Python loader.
    const id = "id" in raw ? (raw.id as unknown) : derivedId(provider, model);
    if (typeof id !== "string" || !PROFILE_ID_RE.test(id)) return null;

    // Duplicate pair first (same provider/model, even with a different id), then
    // duplicate id (explicit collision or a genuine derived-id hash collision).
    const pair = pairKey(provider, model);
    if (seenPairs.has(pair)) return null;
    seenPairs.add(pair);
    if (seenIds.has(id)) return null;
    seenIds.add(id);

    if (raw.enabled && roles.includes(MAIN_ROLE)) main.push({ provider, model });
  }
  return main;
}

/**
 * Read and validate the catalog. A missing, unreadable or invalid catalog yields `null`
 * (nothing is main eligible) without leaking the path or the file contents.
 */
export function loadMainProfiles(): MainProfileRow[] | null {
  let text: string;
  try {
    text = readFileSync(resolveProfilesFile(), "utf8");
  } catch {
    return null;
  }
  return parseMainProfiles(text);
}

export interface Eligibility {
  /** True only for a catalog row that is enabled, holds `main`, and matches provider+model exactly. */
  eligible: boolean;
  /** True when the catalog itself is missing, unreadable or not in the canonical roles format. */
  catalogUnavailable: boolean;
}

export function eligibilityFor(model: { provider?: string; id?: string } | undefined): Eligibility {
  const provider = model?.provider;
  const id = model?.id;
  if (typeof provider !== "string" || !provider) return { eligible: false, catalogUnavailable: false };
  if (typeof id !== "string" || !id) return { eligible: false, catalogUnavailable: false };
  const main = loadMainProfiles();
  if (!main) return { eligible: false, catalogUnavailable: true };
  return {
    eligible: main.some((row) => row.provider === provider && row.model === id),
    catalogUnavailable: false,
  };
}

/** Owned skill paths whose gate opens for the active model, per the roles catalog. */
export function skillsForModel(ctx: ExtensionContext): string[] {
  if (!eligibilityFor(ctx.model).eligible) return [];
  return existsSync(GATED_SKILL_PATH) ? [GATED_SKILL_PATH] : [];
}

function describeSkill(skillPath: string): string {
  const parts = skillPath.split(/[\\/]/);
  return parts[parts.length - 2] ?? skillPath;
}

/** Minimal single-line frontmatter parse (name, description); returns null when unusable. */
function parseSkillFrontmatter(skillPath: string): { name: string; description: string } | null {
  try {
    const text = readFileSync(skillPath, "utf8");
    const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
    if (!match) return null;
    const nameMatch = match[1].match(/^name:\s*(\S+)\s*$/m);
    const descriptionMatch = match[1].match(/^description:\s*(.+)$/m);
    if (!nameMatch || !descriptionMatch) return null;
    const description = descriptionMatch[1].trim();
    if (!description) return null;
    return { name: nameMatch[1]!, description };
  } catch {
    return null;
  }
}

/** Build a minimal Skill entry for prompt injection from a gated SKILL.md. */
export function buildSkillEntry(skillPath: string): Skill | null {
  const frontmatter = parseSkillFrontmatter(skillPath);
  if (!frontmatter) return null;
  return {
    name: frontmatter.name,
    description: frontmatter.description,
    filePath: skillPath,
    baseDir: dirname(skillPath),
    sourceInfo: {
      path: skillPath,
      source: "model-gated-skills",
      scope: "user",
      origin: "top-level",
    },
    disableModelInvocation: false,
  };
}

const CATALOG_WARNING =
  "Gated skill hidden: the pi-worker roles catalog is missing, unreadable or not in the canonical format";

export default function modelGatedSkills(pi: ExtensionAPI) {
  let loaded: string[] = [];
  let announced: string[] = [];
  // One-shot, content-free notices so a broken catalog explains the hidden skill
  // without spamming every turn.
  const warned = new Set<string>();

  function notify(ctx: ExtensionContext, message: string, level: "info" | "warning"): void {
    if (!ctx.hasUI || typeof ctx.ui?.notify !== "function") return;
    try {
      ctx.ui.notify(message, level);
    } catch {
      // A failing or absent UI must never break a turn.
    }
  }

  function warnOnce(ctx: ExtensionContext, key: string, message: string): void {
    if (warned.has(key)) return;
    warned.add(key);
    notify(ctx, message, "warning");
  }

  pi.on("resources_discover", async (_event, ctx) => {
    loaded = skillsForModel(ctx);
    announced = [...loaded];
    if (loaded.length === 0) {
      if (eligibilityFor(ctx.model).catalogUnavailable) {
        warnOnce(ctx, "catalog-unavailable", CATALOG_WARNING);
      }
      return {};
    }
    notify(ctx, `Gated skill available: ${loaded.map(describeSkill).join(", ")}`, "info");
    return { skillPaths: loaded };
  });

  pi.on("before_agent_start", async (event, ctx) => {
    const wanted = skillsForModel(ctx);
    const options = event.systemPromptOptions;
    if (!options || !Array.isArray(options.skills)) return;

    // Drop gated skills that no longer match the active model (or whose catalog row
    // was disabled/edited between turns). Unrelated skills are never touched.
    for (let index = options.skills.length - 1; index >= 0; index -= 1) {
      const skill = options.skills[index]!;
      if (skill?.filePath === GATED_SKILL_PATH && !wanted.includes(skill.filePath)) {
        options.skills.splice(index, 1);
      }
    }

    // Add matching gated skills that are missing (dedupe by filePath).
    const present = new Set(options.skills.map((skill) => skill.filePath));
    const added: string[] = [];
    const removed = announced.filter((path) => !wanted.includes(path));
    for (const skillPath of wanted) {
      if (present.has(skillPath)) continue;
      const entry = buildSkillEntry(skillPath);
      if (!entry) {
        warnOnce(ctx, "unreadable-frontmatter", `Gated skill ${describeSkill(skillPath)} has unreadable frontmatter; skipped`);
        continue;
      }
      options.skills.push(entry);
      present.add(skillPath);
      added.push(describeSkill(skillPath));
    }

    if (added.length > 0 || removed.length > 0) {
      const changes = [
        ...added.map((name) => `+${name}`),
        ...removed.map((name) => `-${name}`),
      ].join(", ");
      notify(ctx, `Gated skills ${changes} (applies now)`, "info");
    }
    announced = [...wanted];
  });

  pi.on("model_select", async (_event, ctx) => {
    const wanted = skillsForModel(ctx);
    const changed =
      wanted.length !== loaded.length || wanted.some((skillPath, index) => skillPath !== loaded[index]);
    if (changed) {
      notify(ctx, `Gated skills ${wanted.length ? "available" : "hidden"}; applies on the next turn`, "info");
    }
    loaded = wanted;
  });
}
