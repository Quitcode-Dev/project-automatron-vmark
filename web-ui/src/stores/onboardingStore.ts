import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

/**
 * Onboarding state — persisted to localStorage.
 *
 * There is no server-side user record (auth is a stateless JWT + email
 * allowlist), so "has this user seen the tour" is tracked client-side, keyed by
 * the session email so a shared machine doesn't hide the tour from a second user.
 * `replayNonce` is ephemeral (not persisted): bumping it asks the mounted
 * TourProvider to (re)start the walkthrough for the current route.
 */
interface OnboardingState {
  seenTourByEmail: Record<string, boolean>;
  replayNonce: number;
  checklistDismissed: boolean;
  hasSeenTour: (email: string | null | undefined) => boolean;
  markTourSeen: (email: string | null | undefined) => void;
  dismissChecklist: () => void;
  requestReplay: () => void;
}

const keyFor = (email: string | null | undefined) => email || "_local";

// SSR-safe: localStorage doesn't exist on the server, so hand persist an
// undefined store there (it no-ops and hydrates on the client).
const safeStorage = createJSONStorage(() =>
  typeof window !== "undefined"
    ? window.localStorage
    : (undefined as unknown as Storage)
);

export const useOnboardingStore = create<OnboardingState>()(
  persist(
    (set, get) => ({
      seenTourByEmail: {},
      checklistDismissed: false,
      replayNonce: 0,
      hasSeenTour: (email) => Boolean(get().seenTourByEmail[keyFor(email)]),
      markTourSeen: (email) =>
        set((s) => ({
          seenTourByEmail: { ...s.seenTourByEmail, [keyFor(email)]: true },
        })),
      dismissChecklist: () => set({ checklistDismissed: true }),
      requestReplay: () => set((s) => ({ replayNonce: s.replayNonce + 1 })),
    }),
    {
      name: "automatron-onboarding",
      storage: safeStorage,
      // Durable state only; replayNonce must not persist.
      partialize: (s) => ({
        seenTourByEmail: s.seenTourByEmail,
        checklistDismissed: s.checklistDismissed,
      }),
    }
  )
);
