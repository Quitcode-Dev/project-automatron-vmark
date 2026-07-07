"use client";

import { Sidebar } from "./Sidebar";
import { Header } from "./Header";
import { TourProvider } from "@/components/onboarding/TourProvider";

interface AppLayoutProps {
  children: React.ReactNode;
}

export function AppLayout({ children }: AppLayoutProps) {
  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <Header />
        <main className="flex-1 overflow-auto p-6">{children}</main>
      </div>
      <TourProvider />
    </div>
  );
}
