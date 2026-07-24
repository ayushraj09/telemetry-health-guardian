"use client";

import { Moon, Stethoscope, Sun } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect } from "react";
import "./globals.css";
import { ApiStatusPill } from "@/components/product/api-status-pill";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { CommandPalette } from "@/components/ui/command";
import { ToastProvider } from "@/components/ui/toast";
import { QueryProvider } from "@/lib/query-client";
import { useGuardianStore } from "@/lib/store";

const nav = [
  { href: "/", label: "Overview" },
  { href: "/rules", label: "Rules" },
  { href: "/chat", label: "Ask Guardian" },
  { href: "/about", label: "How it works" },
];

function Chrome({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const theme = useGuardianStore((state) => state.theme);
  const setTheme = useGuardianStore((state) => state.setTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Link className="brand" href="/">
          <span className="logo-mark"><Stethoscope size={19} /></span>
          <span>Telemetry Health Guardian</span>
        </Link>
        <nav>
          {nav.map((item) => (
            <Link className={pathname === item.href ? "nav-link active" : "nav-link"} href={item.href} key={item.href}>
              {item.label}
            </Link>
          ))}
        </nav>
        <Combobox />
        <CommandPalette />
        <Button onClick={() => setTheme(theme === "dark" ? "light" : "dark")} variant="ghost">
          {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
          {theme === "dark" ? "Light theme" : "Dark theme"}
        </Button>
        <ApiStatusPill />
      </aside>
      <main className="main-panel">{children}</main>
    </div>
  );
}

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html data-theme="dark" lang="en">
      <body>
        <QueryProvider>
          <ToastProvider>
            <Chrome>{children}</Chrome>
          </ToastProvider>
        </QueryProvider>
      </body>
    </html>
  );
}
