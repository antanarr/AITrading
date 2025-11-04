import * as functions from "firebase-functions";
import * as admin from "firebase-admin";
import corsLib from "cors";
import fetch from "node-fetch";
import { z } from "zod";
import { AstroTime, EclipticLongitude, Body } from "astronomy-engine";

admin.initializeApp();

const db = admin.firestore();
const cors = corsLib({ origin: true });

const OPENAI = process.env.OPENAI_API_KEY as string;
const OPENAI_BASE = process.env.OPENAI_BASE || "https://api.openai.com";

if (!OPENAI) console.warn("OPENAI_API_KEY not set; Oracle endpoints will 503");

// ---------- Schemas ----------

const ExtractSchema = z.object({
  symbols: z.array(z.object({ name: z.string(), count: z.number().int().nonnegative() })),
  tone: z.string(),
  archetypes: z.array(z.string())
});

const InterpretSchema = z.object({
  shortSummary: z.string(),
  longForm: z.string(),
  actionPrompt: z.string(),
  symbolCards: z.array(z.object({
    name: z.string(),
    meaning: z.string(),
    personalNote: z.string().optional()
  }))
});

const ChatSchema = z.object({
  reply: z.string(),
  followupPrompt: z.string().optional(),
  warnings: z.array(z.string()).optional()
});

const HoroscopeSchema = z.object({
  range: z.enum(["day", "week", "month", "year"]),
  items: z.array(z.object({
    dateISO: z.string(),
    headline: z.string(),
    bullets: z.array(z.string())
  }))
});

// ---------- Helpers ----------

async function callResponses(model: string, input: any): Promise<any> {
  const res = await fetch(`${OPENAI_BASE}/v1/responses`, {
    method: "POST",
    headers: { "Authorization": `Bearer ${OPENAI}`, "Content-Type": "application/json" },
    body: JSON.stringify({ model, ...input, max_output_tokens: 1200 })
  });
  if (!res.ok) {
    const body = await res.text();
    throw new functions.https.HttpsError("internal", `OpenAI error ${res.status}: ${body}`);
  }
  return await res.json();
}

// Minimal symbol lexicon seed (extend via Firestore later if desired)
const SYMBOL_NOTES: Record<string, string> = {
  water: "Emotion seeking motion; tides of feeling, permeability, intuition.",
  door: "Threshold between familiar and new; permission; agency over entry.",
  room: "Compartmentalized psyche; safety or secrecy; defined boundaries.",
  house: "Self-structure; identity, family, security, roles.",
  bird: "Message, perspective, aspiration; lightness vs tether.",
  teeth: "Power/articulation; vulnerability about expression or appearance.",
  flight: "Freedom vs avoidance; higher vantage; exhilaration/anxiety.",
  ocean: "Vast unconscious; awe, dissolution of edges; depth of feeling."
};

// ---------- Aspect math ----------

const bodies: Body[] = ["Sun", "Moon", "Mercury", "Venus", "Mars"] as any;

function eclLon(body: Body, time: Date): number {
  const t = new AstroTime(time);
  return EclipticLongitude(body, t); // 0..360
}

function degDiff(a: number, b: number): number {
  let d = Math.abs(a - b) % 360;
  return d > 180 ? 360 - d : d;
}

const aspects = [
  { name: "conjunction", angle: 0, orb: 6 },
  { name: "sextile", angle: 60, orb: 4 },
  { name: "square", angle: 90, orb: 6 },
  { name: "trine", angle: 120, orb: 6 },
  { name: "opposition", angle: 180, orb: 6 }
];

function summarizeTransits(birthISO: string, dateISO: string) {
  const birth = new Date(birthISO);
  const now = new Date(dateISO);
  const natal: Record<string, number> = {};
  const current: Record<string, number> = {};

  for (const b of bodies) {
    natal[b as string] = eclLon(b, birth);
    current[b as string] = eclLon(b, now);
  }

  const hits: Array<{ pair: string; aspect: string; orb: number }> = [];

  for (const fast of bodies) {
    for (const natalBody of bodies) {
      const diff = degDiff(current[fast as string], natal[natalBody as string]);
      for (const a of aspects) {
        const delta = Math.abs(diff - a.angle);
        if (delta <= a.orb) {
          hits.push({ pair: `${fast}-${natalBody}`, aspect: a.name, orb: +delta.toFixed(1) });
        }
      }
    }
  }

  const headline = hits[0] ? `${hits[0].pair.replace("-", " ")} ${hits[0].aspect}` : "Quiet sky";
  const notes = hits.slice(0, 4).map(h => `${h.pair.replace("-", " ")} ${h.aspect} (orb ${h.orb}°)`);

  return { headline, notes };
}

// ---------- Scope gate (classifier) ----------

export const scopeGate = functions.https.onRequest(async (req, res) => {
  return cors(req, res, async () => {
    if (!OPENAI) return res.status(503).json({ error: "LLM unavailable" });

    const { text, model = "gpt-4.1-mini" } = req.body || {};

    const prompt = [
      { role: "system", content: "Classify whether the user message is in-scope for Dreamline (dream symbols, emotions, subconscious motifs, or the supplied transits). OUT-OF-SCOPE: politics/news/medical/legal/finance requests or general life coaching without dream content. Respond as JSON {inScope:boolean, reason:string} only." },
      { role: "user", content: String(text ?? "") }
    ];

    const r = await callResponses(model, {
      input: prompt,
      response_format: {
        type: "json_schema",
        json_schema: {
          name: "ScopeGate",
          schema: {
            type: "object",
            properties: {
              inScope: { type: "boolean" },
              reason: { type: "string" }
            },
            required: ["inScope", "reason"],
            additionalProperties: false
          },
          strict: true
        }
      }
    });

    const textOut = r.output_text ?? (r.output?.[0]?.content?.[0]?.text ?? "{}");
    try {
      res.json(JSON.parse(textOut));
    } catch {
      res.json({ inScope: true, reason: "fallback" });
    }
  });
});

// ---------- Oracle: extract ----------

export const oracleExtract = functions.https.onRequest(async (req, res) => {
  return cors(req, res, async () => {
    if (!OPENAI) return res.status(503).json({ error: "LLM unavailable" });

    const dream = String(req.body?.dream || "");
    const model = String(req.body?.model || "gpt-4.1-mini");

    const system = "You are Dreamline's extraction engine. Detect concrete dream symbols (nouns/noun phrases), the emotional tone, and 1–3 archetypes. Be conservative; do not invent. Use canonical symbols when possible (e.g., water, door, room, house, flight, bird, ocean, teeth). Tone is a single lowercase descriptor. Archetypes from a small lexicon (threshold, journey, shadow, rebirth, loss, security, transformation). Output schema exactly.";

    const schema = {
      type: "object",
      properties: {
        symbols: {
          type: "array",
          items: {
            type: "object",
            properties: {
              name: { type: "string" },
              count: { type: "integer", minimum: 0 }
            },
            required: ["name", "count"],
            additionalProperties: false
          }
        },
        tone: { type: "string" },
        archetypes: { type: "array", items: { type: "string" } }
      },
      required: ["symbols", "tone", "archetypes"],
      additionalProperties: false
    };

    const prompt = [
      { role: "system", content: system },
      { role: "user", content: dream }
    ];

    const r = await callResponses(model, {
      input: prompt,
      response_format: {
        type: "json_schema",
        json_schema: { name: "OracleExtraction", schema, strict: true }
      }
    });

    const text = r.output_text ?? (r.output?.[0]?.content?.[0]?.text ?? "");
    const parsed = ExtractSchema.safeParse(JSON.parse(text));

    if (!parsed.success) return res.status(422).json({ error: parsed.error.flatten() });

    res.json(parsed.data);
  });
});

// ---------- Oracle: interpret (history + transits) ----------

export const oracleInterpret = functions.https.onRequest(async (req, res) => {
  return cors(req, res, async () => {
    if (!OPENAI) return res.status(503).json({ error: "LLM unavailable" });

    const { dream, extraction, transit, history, model = "gpt-4.1-mini" } = req.body || {};

    const system =
`You are the Dreamline Oracle. Write brief, reflective readings that connect the user's dream symbols with
their ongoing motifs and today's transits. Stay grounded in the provided dream text, extracted symbols,
history, and transit summary.

VOICE:
- Address the user as "you"; calm, lyrical but clear. Avoid clichés; one vivid image max.
- Short summary: 1–2 sentences. Long form: 3–6 sentences (≈120–180 words). End with one concrete micro‑action.

RESONANCE:
- Mirror one phrase from the dream/history (e.g., "locked room", "high water").
- Name one universal tension bound to this dream (safety↔change, control↔surrender) using the given symbols.
- Tie one symbol to the transit headline as an influence (no predictions).

GUARDRAILS:
- No deterministic forecasts, medical/legal/financial advice, diagnosis, or real‑world names not in the dream.
- If the input is off‑topic, gently reframe toward symbol work.

OUTPUT:
Follow the JSON schema exactly (shortSummary, longForm, actionPrompt, symbolCards[]). Keep symbol meanings concise. Use SYMBOL_NOTES when relevant.`;

    const schema = {
      type: "object",
      properties: {
        shortSummary: { type: "string" },
        longForm: { type: "string" },
        actionPrompt: { type: "string" },
        symbolCards: {
          type: "array",
          items: {
            type: "object",
            properties: {
              name: { type: "string" },
              meaning: { type: "string" },
              personalNote: { type: "string" }
            },
            required: ["name", "meaning"],
            additionalProperties: false
          }
        }
      },
      required: ["shortSummary", "longForm", "actionPrompt", "symbolCards"],
      additionalProperties: false
    };

    const grounding = "Symbol lexicon hints: " + Object.entries(SYMBOL_NOTES).map(([k, v]) => `${k}: ${v}`).join(" | ");

    const prompt = [
      { role: "system", content: system + "\n" + grounding },
      { role: "user", content: `Dream: ${dream}` },
      { role: "user", content: `Extraction: ${JSON.stringify(extraction)}` },
      { role: "user", content: `Transit: ${JSON.stringify(transit)}` },
      { role: "user", content: `History: ${JSON.stringify(history ?? {})}` }
    ];

    const r = await callResponses(model, {
      input: prompt,
      response_format: {
        type: "json_schema",
        json_schema: { name: "OracleInterpretation", schema, strict: true }
      }
    });

    const text = r.output_text ?? (r.output?.[0]?.content?.[0]?.text ?? "");
    const parsed = InterpretSchema.safeParse(JSON.parse(text));

    if (!parsed.success) return res.status(422).json({ error: parsed.error.flatten() });

    res.json(parsed.data);
  });
});

// ---------- Oracle: chat (Pro) with scope gate ----------

export const oracleChat = functions.https.onRequest(async (req, res) => {
  return cors(req, res, async () => {
    if (!OPENAI) return res.status(503).json({ error: "LLM unavailable" });

    const { messages, history, dreamContext, transit, model = "gpt-4.1-mini" } = req.body || {};

    // Pre-gate
    const gate = await callResponses(model, {
      input: [
        { role: "system", content: "Return JSON {inScope:boolean, reason:string} for dream/astrology relevance." },
        { role: "user", content: String(messages?.slice(-1)?.[0]?.content ?? "") }
      ],
      response_format: {
        type: "json_schema",
        json_schema: {
          name: "ScopeGate",
          schema: {
            type: "object",
            properties: {
              inScope: { type: "boolean" },
              reason: { type: "string" }
            },
            required: ["inScope", "reason"],
            additionalProperties: false
          },
          strict: true
        }
      }
    });

    const gateText = gate.output_text ?? (gate.output?.[0]?.content?.[0]?.text ?? "{}");
    let inScope = true;
    try {
      inScope = JSON.parse(gateText).inScope;
    } catch {}

    if (!inScope) {
      return res.json({
        reply: "I focus on dream symbols, inner patterns, and today's sky. Share a dream line or symbol and I'll help interpret.",
        followupPrompt: "What symbol stands out from your last dream?",
        warnings: ["out_of_scope"]
      });
    }

    const system =
`You are the Dreamline Oracle Chat. Stay strictly anchored to dream symbols, emotions, archetypes, and the provided transits/history.

No predictions or advice in medical/legal/financial domains. Keep responses 2–5 sentences, end with one reflective question.`;

    const payload = [
      { role: "system", content: system },
      { role: "user", content: `Transit: ${JSON.stringify(transit ?? {})}` },
      { role: "user", content: `History: ${JSON.stringify(history ?? {})}` },
      ...(dreamContext ? [{ role: "user", content: `Dream context: ${dreamContext}` }] : []),
      ...((messages ?? []) as Array<{ role: string; content: string }>)
    ];

    const r = await callResponses(model, {
      input: payload,
      response_format: {
        type: "json_schema",
        json_schema: {
          name: "OracleChatReply",
          schema: {
            type: "object",
            properties: {
              reply: { type: "string" },
              followupPrompt: { type: "string" }
            },
            required: ["reply"],
            additionalProperties: false
          },
          strict: true
        }
      }
    });

    const text = r.output_text ?? (r.output?.[0]?.content?.[0]?.text ?? "");
    const parsed = ChatSchema.safeParse(JSON.parse(text));

    if (!parsed.success) return res.status(422).json({ error: parsed.error.flatten() });

    res.json(parsed.data);
  });
});

// ---------- Astro: range (day/week/month/year) ----------

export const astroTransitsRange = functions.https.onRequest(async (req, res) => {
  return cors(req, res, async () => {
    const { birthISO, startISO, endISO, step = "day", range = "day" } = req.body || {};

    if (!birthISO || !startISO || !endISO) {
      return res.status(400).json({ error: "birthISO, startISO, endISO required" });
    }

    const start = new Date(String(startISO));
    const end = new Date(String(endISO));

    const items: Array<{ dateISO: string; headline: string; bullets: string[] }> = [];

    for (let d = new Date(start); d <= end; d = new Date(d.getTime() + 86400_000)) {
      const s = summarizeTransits(birthISO, d.toISOString());
      items.push({ dateISO: d.toISOString(), headline: s.headline, bullets: s.notes });

      if (range === "day") break;
    }

    res.json({ range, items });
  });
});

// ---------- Horoscope: compose text from transits ----------

export const horoscopeCompose = functions.https.onRequest(async (req, res) => {
  return cors(req, res, async () => {
    if (!OPENAI) return res.status(503).json({ error: "LLM unavailable" });

    const { range, items, model = "gpt-4.1-mini" } = req.body || {};

    const sys =
`You write succinct, grounded horoscope guidance based on transit headlines/bullets already computed.
Tone: calm, reflective, poetic‑practical. No predictions or fate claims; no health/finance/legal advice.
For 'day' return 1–2 sentences. For 'week' return 3 bullets. For 'month' return 4–6 bullets. For 'year' return 5–8 bullets.`;

    const prompt = [
      { role: "system", content: sys },
      { role: "user", content: `Range: ${range}` },
      { role: "user", content: `Transits: ${JSON.stringify(items)}` }
    ];

    const r = await callResponses(model, { input: prompt });

    const text = r.output_text ?? (r.output?.[0]?.content?.[0]?.text ?? "");

    res.json({ range, text });
  });
});

// ---------- Usage counter (atomic) ----------

export const incrementUsage = functions.https.onRequest(async (req, res) => {
  return cors(req, res, async () => {
    const { uid, key } = req.body || {};

    if (!uid || !key) return res.status(400).json({ error: "uid and key required" });

    const ref = db.collection("users").doc(uid).collection("usage").doc(key);

    await db.runTransaction(async (tx) => {
      const snap = await tx.get(ref);
      const count = (snap.exists ? (snap.get("count") as number) : 0) + 1;
      tx.set(ref, { count, updatedAt: admin.firestore.FieldValue.serverTimestamp() }, { merge: true });
    });

    res.json({ ok: true });
  });
});

