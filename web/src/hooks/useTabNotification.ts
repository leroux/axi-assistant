import { useCallback, useEffect, useRef } from "react";

const ORIGINAL_TITLE = "Axi Web";

/**
 * Flashes the browser tab title when new messages arrive while the tab is hidden.
 * Returns a notify() function to trigger the flash.
 */
export function useTabNotification() {
  const hiddenRef = useRef(false);
  const flashRef = useRef<ReturnType<typeof setInterval>>(undefined);
  const unreadRef = useRef(0);

  useEffect(() => {
    const onVisibility = () => {
      hiddenRef.current = document.hidden;
      if (!document.hidden) {
        // Tab became visible — stop flashing
        clearInterval(flashRef.current);
        unreadRef.current = 0;
        document.title = ORIGINAL_TITLE;
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      clearInterval(flashRef.current);
      document.title = ORIGINAL_TITLE;
    };
  }, []);

  const notify = useCallback(() => {
    if (!hiddenRef.current) return;
    unreadRef.current++;

    // Start flashing if not already
    if (!flashRef.current) {
      let on = true;
      flashRef.current = setInterval(() => {
        document.title = on ? `(${unreadRef.current}) New message` : ORIGINAL_TITLE;
        on = !on;
      }, 1000);
    }
  }, []);

  return notify;
}
