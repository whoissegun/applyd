import Link from "next/link";
import { SignupForm } from "./form";

export default function SignupPage() {
  return (
    <main className="theme-eleven flex-1 flex flex-col">
      <header
        className="flex items-center justify-between px-6 md:px-12"
        style={{ height: 56, borderBottom: "1px solid #e5e5e5" }}
      >
        <Link href="/" style={{ fontFamily: "var(--font-cormorant)", fontWeight: 400, fontSize: 22, letterSpacing: "-0.01em", color: "#000" }}>
          applyd
        </Link>
        <Link href="/login" className="pill pill-ghost">
          Log in
        </Link>
      </header>

      <section className="flex-1 flex items-center justify-center px-6 py-16">
        <div className="w-full max-w-[480px] flex flex-col gap-10">
          <div>
            <p className="eyebrow">Get started</p>
            <h1 className="h-eleven mt-3">Create your account.</h1>
            <p className="muted mt-4" style={{ fontSize: 16, lineHeight: 1.5 }}>
              One resume in. Hundreds of applications out.
            </p>
          </div>

          <div className="card">
            <SignupForm />
          </div>

          <p className="muted" style={{ fontSize: 14, textAlign: "center" }}>
            Already have an account?{" "}
            <Link href="/login" style={{ color: "#000", textDecoration: "underline", textUnderlineOffset: 4 }}>
              Log in
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
