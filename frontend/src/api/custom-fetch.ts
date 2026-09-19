import axios, { type AxiosRequestConfig, type AxiosError } from "axios";
import { useAuthStore, type UserRole } from "@/features/auth/auth-store";
import { decodeJwtPayload } from "@/lib/jwt";

const apiClient = axios.create({
  baseURL: "/api",
});

const VALID_ROLES: UserRole[] = [
  "trainer",
  "student",
  "parent",
  "owner",
  "admin",
];

function syncMembershipFromAccessToken(accessToken: string) {
  const payload = decodeJwtPayload(accessToken);
  const role = payload?.role;
  const clubId = payload?.club_id;

  if (
    typeof role === "string" &&
    VALID_ROLES.includes(role as UserRole) &&
    typeof clubId === "number"
  ) {
    const nextRole = role as UserRole;
    useAuthStore.setState({
      role: nextRole,
      clubId,
      trainerId: null,
      studentId: null,
      studentBootstrapStatus: nextRole === "student" ? "idle" : "resolved",
      studentBootstrapError: null,
    });
    return;
  }

  useAuthStore.setState({
    role: null,
    clubId: null,
    trainerId: null,
    studentId: null,
    studentBootstrapStatus: "idle",
    studentBootstrapError: null,
  });
}

// Request interceptor: attach access token from Zustand store
apiClient.interceptors.request.use((config) => {
  const token = useAuthStore.getState().accessToken;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

async function performRefreshAccessToken(): Promise<string> {
  const response = await axios.post("/api/auth/refresh/", null, {
    withCredentials: true,
  });
  const { access_token } = response.data;
  useAuthStore.getState().setTokens(access_token);
  syncMembershipFromAccessToken(access_token);
  return access_token;
}

let refreshPromise: Promise<string> | null = null;

export function refreshAccessToken(
  legacyRefreshToken?: string | null,
): Promise<string> {
  void legacyRefreshToken;
  if (!refreshPromise) {
    refreshPromise = performRefreshAccessToken().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

export async function storeRefreshTokenCookie(
  refreshToken: string,
  accessToken: string,
): Promise<void> {
  await axios.post(
    "/api/auth/refresh-cookie/",
    { refresh_token: refreshToken },
    {
      headers: { Authorization: `Bearer ${accessToken}` },
      withCredentials: true,
    },
  );
}

apiClient.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    if (error.response?.status !== 401) throw error;

    const store = useAuthStore.getState();
    if (!store.refreshToken) {
      store.logout();
      throw error;
    }

    try {
      const newToken = await refreshAccessToken(store.refreshToken);
      if (!error.config) {
        throw new Error("Cannot retry request: original config missing");
      }
      if (!useAuthStore.getState().isAuthenticated) {
        throw error;
      }
      error.config.headers.Authorization = `Bearer ${newToken}`;
      return apiClient(error.config);
    } catch (refreshError) {
      useAuthStore.getState().logout();
      throw refreshError;
    }
  },
);

// Orval mutator signature -- all generated hooks use this
export const customFetch = <T>(config: AxiosRequestConfig): Promise<T> => {
  return apiClient(config).then((response) => response.data);
};

export default apiClient;
