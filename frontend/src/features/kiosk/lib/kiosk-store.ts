import { create } from "zustand";
import { clearKioskOfflineData } from "./kiosk-db";

const LS_TOKEN = "kiosk_device_token";
const LS_CLUB_ID = "kiosk_club_id";
const LS_CLUB_NAME = "kiosk_club_name";

interface KioskState {
  deviceToken: string | null;
  clubId: number | null;
  clubName: string | null;
  isActivated: boolean;
  activate: (token: string, clubId: number, clubName: string) => Promise<void>;
  deactivate: (options?: { preservePending?: boolean }) => Promise<void>;
}

export const useKioskStore = create<KioskState>()((set) => ({
  deviceToken: localStorage.getItem(LS_TOKEN),
  clubId: (() => {
    const v = localStorage.getItem(LS_CLUB_ID);
    if (!v) return null;
    const n = parseInt(v, 10);
    return Number.isNaN(n) ? null : n;
  })(),
  clubName: localStorage.getItem(LS_CLUB_NAME),
  isActivated: !!localStorage.getItem(LS_TOKEN),

  activate: async (token, clubId, clubName) => {
    const previousClubId = useKioskStore.getState().clubId;
    await clearKioskOfflineData({
      preservePending: previousClubId === clubId,
    });
    localStorage.setItem(LS_TOKEN, token);
    localStorage.setItem(LS_CLUB_ID, String(clubId));
    localStorage.setItem(LS_CLUB_NAME, clubName);
    set({ deviceToken: token, clubId, clubName, isActivated: true });
  },

  deactivate: async (options = {}) => {
    localStorage.removeItem(LS_TOKEN);
    localStorage.removeItem(LS_CLUB_ID);
    localStorage.removeItem(LS_CLUB_NAME);
    set({
      deviceToken: null,
      clubId: null,
      clubName: null,
      isActivated: false,
    });
    await clearKioskOfflineData(options);
  },
}));
