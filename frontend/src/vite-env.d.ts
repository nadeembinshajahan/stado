/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_GOOGLE_MAPS_API_KEY?: string;
  readonly VITE_GOOGLE_MAPS_MAP_ID?: string;
  readonly VITE_GO2RTC_URL?: string;
  readonly VITE_GO2RTC_STREAM?: string;
  readonly VITE_BACKEND?: string;
  readonly VITE_GMAPS_VERSION?: string;
  readonly VITE_GOOGLE_MAPS_MAP_ID?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
