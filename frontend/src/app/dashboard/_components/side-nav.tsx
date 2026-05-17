"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const ITEMS = [
  { href: "/dashboard", label: "Overview" },
  { href: "/dashboard/applications", label: "Applications" },
  { href: "/dashboard/resume", label: "Master resume" },
  { href: "/dashboard/profile", label: "Profile" },
];

export function SideNav() {
  const pathname = usePathname();
  return (
    <nav className="flex flex-col gap-px">
      {ITEMS.map((item) => {
        const active =
          item.href === "/dashboard"
            ? pathname === "/dashboard"
            : pathname.startsWith(item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            className="px-2 h-8 flex items-center rounded-[8px] transition-colors"
            style={{
              fontSize: "var(--text-sm)",
              color: active ? "var(--color-ghost)" : "var(--color-pebble)",
              background: active ? "var(--color-whisper-strong)" : "transparent",
            }}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
