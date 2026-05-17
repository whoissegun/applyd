/**
 * Server-only: extract a resume PDF into {text, contact, workAuth} via Claude.
 *
 * The tailor consumes plain text. Contact + work_auth are tiny prefill blobs
 * for the onboarding basics + work-auth fields. We deliberately don't return
 * a full JSON schema — see the conversation in TODO.md / CLAUDE.md.
 *
 * Format-mirror of src/applyd/onboarding/extract.py. Keep them in sync.
 */

import Anthropic from "@anthropic-ai/sdk";

export type Contact = {
  name: string | null;
  phone: string | null;
  email: string | null;
  linkedin_url: string | null;
  github_url: string | null;
  portfolio_url: string | null;
};

export type WorkAuth = {
  summary: string | null;
  sponsorship_needed_countries: string[];
};

export type ExtractedResume = {
  text: string;
  contact: Contact;
  workAuth: WorkAuth;
  targetRoles: string | null;
};

const SYSTEM_PROMPT = `You extract content from an attached resume PDF. Return
exactly four sections in this order, each starting at column zero with the
section header on its own line:

CONTACT
name: <full name>
phone: <phone or empty>
email: <email or empty>
linkedin: <full URL or empty>
github: <full URL or empty>
portfolio: <full URL or empty>

WORK_AUTH
summary: <one-line statement of work authorization if the resume mentions it, else empty>
sponsorship_needed: <comma-separated country names if mentioned, else empty>

TARGET_ROLES
roles: <one or two sentences inferring the kinds of roles this person is best
suited for, based purely on their experience, projects, and skills. Be specific:
discipline (e.g. backend, ML infra, full-stack, applied ML, frontend), seniority
(intern / new grad / mid / senior — pick from their dates and titles), and any
strong domain signal (e.g. AI labs, fintech, infra). One direct statement. No
hedging. Example: "New-grad SWE leaning backend / ML infra, strong React +
TypeScript on the side. Also a fit for applied-ML at AI labs." If the resume
is too sparse to tell, write "Hard to tell from this resume — needs the user's
input.">

TEXT
<the entire resume as plain text, kitchen-sink — every role, project, bullet,
skill. Preserve paragraph breaks. No markdown, no LaTeX, no headers like
"EXPERIENCE" in caps — just sections with natural casing. Bullets become
one-line statements prefixed with "- ". Numbers and units stay verbatim.>

Do not omit anything from TEXT. If a field in CONTACT or WORK_AUTH isn't on
the resume, leave it empty after the colon. Do not invent facts. TARGET_ROLES
is your inference, not a fact-extraction — that's allowed there only.`;

const MODEL = "claude-sonnet-4-6";

const SECTION_RE = /^(CONTACT|WORK_AUTH|TARGET_ROLES|TEXT)\s*$/gm;

function splitSections(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  const matches = [...raw.matchAll(SECTION_RE)];
  for (let i = 0; i < matches.length; i++) {
    const m = matches[i];
    const name = m[1];
    const bodyStart = m.index! + m[0].length;
    const bodyEnd =
      i + 1 < matches.length ? matches[i + 1].index! : raw.length;
    out[name] = raw.slice(bodyStart, bodyEnd).trim();
  }
  return out;
}

function parseKv(block: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of block.split("\n")) {
    const m = /^\s*([a-z_]+)\s*:\s*(.*)$/i.exec(line);
    if (m) out[m[1]] = m[2].trim();
  }
  return out;
}

export async function extractResume(opts: {
  pdfBase64: string;
}): Promise<{ ok: true; resume: ExtractedResume } | { ok: false; error: string }> {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) return { ok: false, error: "ANTHROPIC_API_KEY not set" };

  const client = new Anthropic({ apiKey });

  let resp;
  try {
    resp = await client.messages.create({
      model: MODEL,
      max_tokens: 8192,
      temperature: 0,
      system: SYSTEM_PROMPT,
      messages: [
        {
          role: "user",
          content: [
            {
              type: "document",
              source: {
                type: "base64",
                media_type: "application/pdf",
                data: opts.pdfBase64,
              },
            },
            { type: "text", text: "Extract per the schema. No commentary." },
          ],
        },
      ],
    });
  } catch (err) {
    return { ok: false, error: `claude call failed: ${(err as Error).message}` };
  }

  const raw = resp.content
    .filter((b): b is Anthropic.TextBlock => b.type === "text")
    .map((b) => b.text)
    .join("")
    .trim();
  const sections = splitSections(raw);

  const text = (sections["TEXT"] ?? "").trim();
  if (!text) return { ok: false, error: "extractor returned no TEXT section" };

  const ckv = parseKv(sections["CONTACT"] ?? "");
  const contact: Contact = {
    name: ckv.name || null,
    phone: ckv.phone || null,
    email: ckv.email || null,
    linkedin_url: ckv.linkedin || null,
    github_url: ckv.github || null,
    portfolio_url: ckv.portfolio || null,
  };

  const wakv = parseKv(sections["WORK_AUTH"] ?? "");
  const sponsorshipRaw = wakv.sponsorship_needed ?? "";
  const sponsorship = sponsorshipRaw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const workAuth: WorkAuth = {
    summary: wakv.summary || null,
    sponsorship_needed_countries: sponsorship,
  };

  const trkv = parseKv(sections["TARGET_ROLES"] ?? "");
  const targetRoles = trkv.roles || null;

  return { ok: true, resume: { text, contact, workAuth, targetRoles } };
}
