import { Dashboard } from "@/components/dashboard";

export default function Home() {
  return (
    <main className="min-h-screen bg-zinc-950 px-4 py-8 sm:px-8">
      <div className="mx-auto max-w-6xl space-y-6">
        <header>
          <h1 className="font-mono text-xl font-bold tracking-tight text-zinc-100">
            CLEANROOM // MISSION CONTROL
          </h1>
          <p className="font-mono text-xs text-zinc-500">
            adversarial input console — hardened vs. unhardened pipeline
          </p>
        </header>
        <Dashboard />
      </div>
    </main>
  );
}
