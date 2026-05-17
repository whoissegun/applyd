import Link from "next/link";
import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";

export default async function Home() {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (user) redirect("/dashboard");

  return (
    <main className="theme-eleven flex-1 flex flex-col">
      {/* ── NAV ────────────────────────────────────────────────────── */}
      <header
        className="flex items-center justify-between"
        style={{
          height: 64,
          maxWidth: 1200,
          margin: "0 auto",
          width: "100%",
          padding: "0 24px",
        }}
      >
        <Link
          href="/"
          style={{
            fontFamily: "var(--font-cormorant)",
            fontWeight: 400,
            fontSize: 24,
            letterSpacing: "-0.01em",
            color: "#000",
          }}
        >
          applyd
        </Link>
        <div className="flex items-center gap-2">
          <Link href="/login" className="pill pill-ghost">Log in</Link>
          <Link href="/signup" className="pill pill-primary">Sign up</Link>
        </div>
      </header>

      {/* ── HERO ───────────────────────────────────────────────────── */}
      <section
        style={{
          maxWidth: 1200,
          margin: "0 auto",
          width: "100%",
          padding: "100px 24px 80px",
        }}
      >
        <div className="grid grid-cols-1 md:grid-cols-12 gap-12 items-center">
          <div className="md:col-span-7 flex flex-col gap-8">
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 8,
                fontSize: 13,
                fontWeight: 500,
                letterSpacing: "0.03em",
                color: "#000",
              }}
            >
              <span
                aria-hidden
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: 9999,
                  background: "#49de80",
                  boxShadow: "0 0 0 4px rgba(73, 222, 128, 0.18)",
                }}
              />
              Always on · applies 24/7
            </span>
            <h1
              className="h-eleven"
              style={{
                fontSize: "clamp(44px, 6.5vw, 80px)",
                lineHeight: 1.03,
                letterSpacing: "-0.025em",
                maxWidth: "13ch",
              }}
            >
              One resume.{" "}
              <em
                style={{
                  fontStyle: "italic",
                  color: "#777169",
                  fontFamily: "var(--font-cormorant)",
                }}
              >
                Hundreds of applications.
              </em>
            </h1>
            <div style={{ maxWidth: 540 }}>
              <p style={{ color: "#000", fontSize: 18, lineHeight: 1.55 }}>
                applyd finds jobs that match you, writes a custom resume for
                each one, and sends the application for you. You upload one
                resume. That&apos;s all you do.
              </p>
              <p
                style={{
                  fontFamily: "var(--font-cormorant)",
                  fontStyle: "italic",
                  fontWeight: 300,
                  fontSize: 22,
                  lineHeight: 1.3,
                  color: "#000",
                  marginTop: 16,
                  letterSpacing: "-0.01em",
                }}
              >
                Around the clock. Even while you sleep.
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-3 pt-2">
              <Link href="/signup" className="pill pill-primary">Start free</Link>
              <Link href="/login" className="pill pill-ghost">I have an account</Link>
            </div>
          </div>

          <div className="md:col-span-5 md:pl-4">
            <DemoCard />
          </div>
        </div>
      </section>

      {/* ── DIVIDER ────────────────────────────────────────────────── */}
      <div style={{ maxWidth: 1200, margin: "0 auto", width: "100%", padding: "0 24px" }}>
        <div className="hairline-eleven" />
      </div>

      {/* ── HOW IT WORKS — plain language, 3 steps ─────────────────── */}
      <section
        style={{
          maxWidth: 1200,
          margin: "0 auto",
          width: "100%",
          padding: "96px 24px",
        }}
      >
        <p className="eyebrow">How it works</p>
        <h2
          className="h-eleven mt-3"
          style={{
            fontSize: "clamp(32px, 4vw, 44px)",
            letterSpacing: "-0.022em",
            lineHeight: 1.1,
            maxWidth: "18ch",
          }}
        >
          Three steps. Then your inbox does the rest.
        </h2>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-12 mt-16">
          {STEPS.map((s, i) => (
            <article key={s.title} className="flex flex-col gap-3">
              <span
                style={{
                  fontFamily: "var(--font-cormorant)",
                  fontWeight: 300,
                  fontSize: 56,
                  lineHeight: 1,
                  letterSpacing: "-0.03em",
                  color: "#000",
                }}
              >
                {String(i + 1).padStart(2, "0")}
              </span>
              <h3
                style={{
                  fontFamily: "var(--font-inter)",
                  fontWeight: 500,
                  fontSize: 18,
                  color: "#000",
                  lineHeight: 1.3,
                  marginTop: 8,
                }}
              >
                {s.title}
              </h3>
              <p style={{ color: "#777169", fontSize: 15, lineHeight: 1.6 }}>
                {s.body}
              </p>
            </article>
          ))}
        </div>
      </section>

      {/* ── DIVIDER ────────────────────────────────────────────────── */}
      <div style={{ maxWidth: 1200, margin: "0 auto", width: "100%", padding: "0 24px" }}>
        <div className="hairline-eleven" />
      </div>

      {/* ── PRICING ───────────────────────────────────────────────── */}
      <section
        style={{
          maxWidth: 1200,
          margin: "0 auto",
          width: "100%",
          padding: "96px 24px",
        }}
      >
        <div className="text-center flex flex-col items-center gap-6">
          <p className="eyebrow">Pricing</p>
          <h2
            className="h-eleven"
            style={{
              fontSize: "clamp(36px, 5vw, 56px)",
              letterSpacing: "-0.025em",
              lineHeight: 1.05,
              maxWidth: "20ch",
            }}
          >
            Free until you&apos;re applying every day.
          </h2>
          <p style={{ color: "#777169", fontSize: 17, lineHeight: 1.6, maxWidth: 520 }}>
            50 applications free. Then a flat monthly price. Cancel anytime.
          </p>
          <div className="flex gap-3 pt-4">
            <Link href="/signup" className="pill pill-primary">Start free</Link>
          </div>
        </div>
      </section>

      {/* ── FOOTER ────────────────────────────────────────────────── */}
      <footer style={{ borderTop: "1px solid #e5e5e5" }}>
        <div
          style={{
            maxWidth: 1200,
            margin: "0 auto",
            width: "100%",
            padding: "32px 24px",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
            gap: 16,
            fontSize: 13,
            color: "#777169",
          }}
        >
          <span style={{ fontFamily: "var(--font-cormorant)", fontSize: 20, color: "#000" }}>
            applyd
          </span>
          <nav className="flex items-center gap-6">
            <Link href="/login" style={{ color: "#777169" }}>Log in</Link>
            <Link href="/signup" style={{ color: "#777169" }}>Sign up</Link>
          </nav>
        </div>
      </footer>
    </main>
  );
}

function DemoCard() {
  const ROWS = [
    { co: "Stripe",     role: "ML Engineer",          status: "applied",  tone: "#0447ff" },
    { co: "Anthropic",  role: "Research Engineer",    status: "applied",  tone: "#ff4704" },
    { co: "Lyft",       role: "Software Engineer",    status: "in progress", tone: "#777169" },
    { co: "Runway",     role: "Applied ML",           status: "applied",  tone: "#0447ff" },
  ];
  return (
    <div
      style={{
        background: "#fff",
        borderRadius: 16,
        boxShadow:
          "rgba(0,0,0,0.4) 0px 0px 1.143px 0px, rgba(0,0,0,0.04) 0px 2px 4px 0px",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "16px 20px",
          borderBottom: "1px solid #e5e5e5",
        }}
      >
        <span style={{ fontSize: 14, fontWeight: 500, color: "#000" }}>
          This week
        </span>
        <span style={{ fontFamily: "var(--font-geist-mono)", fontSize: 12, color: "#777169" }}>
          4 applied
        </span>
      </div>
      <ul style={{ padding: 8 }}>
        {ROWS.map((r) => (
          <li
            key={r.co + r.role}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              padding: "12px",
              borderRadius: 4,
            }}
          >
            <span
              style={{
                width: 28,
                height: 28,
                borderRadius: 9999,
                background: r.tone,
                flexShrink: 0,
                boxShadow: "inset 0 0 0 1px rgba(0,0,0,0.08)",
              }}
            />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 500, fontSize: 14, color: "#000" }}>{r.co}</div>
              <div
                style={{
                  fontSize: 13,
                  color: "#777169",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {r.role}
              </div>
            </div>
            <span
              style={{
                fontFamily: "var(--font-inter)",
                fontSize: 11,
                fontWeight: 500,
                letterSpacing: "0.04em",
                textTransform: "uppercase",
                color: "#000",
                padding: "4px 10px",
                border: "1px solid #e5e5e5",
                borderRadius: 9999,
              }}
            >
              {r.status}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

const STEPS = [
  {
    title: "Tell us about you.",
    body:
      "Upload your resume and answer a few quick questions about what you're looking for. Five minutes, one time.",
  },
  {
    title: "We find your matches.",
    body:
      "Every day, applyd surfaces fresh roles that fit your background — from companies you've heard of and ones you haven't.",
  },
  {
    title: "We apply for you.",
    body:
      "Each application gets a resume tailored to that role, sent for you. You just check your inbox for replies.",
  },
];
