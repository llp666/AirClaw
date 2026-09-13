"use client";

import { useCallback, useEffect, useRef } from "react";

/** 可拖拽的面板分隔条。 */
export default function ResizeHandle({
  onResize,
}: {
  onResize: (deltaX: number) => void;
}) {
  const dragging = useRef(false);
  const lastX = useRef(0);

  const onPointerMove = useCallback(
    (e: PointerEvent) => {
      if (!dragging.current) return;
      const delta = e.clientX - lastX.current;
      lastX.current = e.clientX;
      onResize(delta);
    },
    [onResize],
  );

  const stop = useCallback(() => {
    dragging.current = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, []);

  useEffect(() => {
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", stop);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", stop);
    };
  }, [onPointerMove, stop]);

  return (
    <div
      onPointerDown={(e) => {
        dragging.current = true;
        lastX.current = e.clientX;
        // 拖拽期间禁用文本选择，否则会选中侧栏内容
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
      }}
      className="group relative w-px shrink-0 cursor-col-resize bg-border"
    >
      {/* 命中区域比视觉宽度大，便于抓取 */}
      <div className="absolute inset-y-0 -left-1 -right-1 group-hover:bg-accent/25" />
    </div>
  );
}
