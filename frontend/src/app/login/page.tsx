import Link from "next/link";
import { LoginForm } from "./form";

type SearchParams = Promise<{ next?: string }>;

export default async function LoginPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const { next } = await searchParams;
  return (
    <main className="theme-eleven flex-1 flex flex-col">
      {/* Top nav — 36px, eggshell, hairline border-bottom */}
      <header
        className="flex items-center justify-between px-6 md:px-12"
        style={{ height: 56, borderBottom: "1px solid #e5e5e5" }}
      >
        <Link href="/" style={{ fontFamily: "var(--font-cormorant)", fontWeight: 400, fontSize: 22, letterSpacing: "-0.01em", color: "#000" }}>
          applyd
        </Link>
        <Link href="/signup" className="pill pill-ghost">
          Sign up
        </Link>
      </header>

      <section className="flex-1 flex items-center justify-center px-6 py-16">
        <div className="w-full max-w-[440px] flex flex-col gap-10">
          <div>
            <p className="eyebrow">Welcome back</p>
            <h1 className="h-eleven mt-3">Sign in to applyd.</h1>
          </div>

          <div className="card">
            <LoginForm next={next ?? "/dashboard"} />
          </div>

          <p className="muted" style={{ fontSize: 14, textAlign: "center" }}>
            No account yet?{" "}
            <Link href="/signup" style={{ color: "#000", textDecoration: "underline", textUnderlineOffset: 4 }}>
              Create one
            </Link>
          </p>
        </div>
      </section>

      <footer
        className="px-6 md:px-12 py-6 flex items-center justify-between"
        style={{ borderTop: "1px solid #e5e5e5", fontSize: 13, color: "#777169" }}
      >
        <span>applyd</span>
        <Link href="/" style={{ color: "#777169" }}>Back to home</Link>
      </footer>
    </main>
  );
}
