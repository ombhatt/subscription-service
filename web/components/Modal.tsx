"use client";

import { useEffect, useRef } from "react";

/**
 * A native `<dialog>`, opened with `showModal()`.
 *
 * Native rather than a positioned div: the browser supplies the focus trap,
 * Escape to dismiss, inert background content and the dialog role for free.
 * Every hand-rolled version of those is a defect waiting to be found by someone
 * navigating with a keyboard or a screen reader.
 */
export default function Modal({
  open,
  onClose,
  label,
  children,
}: {
  open: boolean;
  onClose: () => void;
  label: string;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      className="modal"
      aria-label={label}
      // Fires for Escape too, so the parent's state follows the dialog rather
      // than the other way round.
      onClose={onClose}
      onClick={(event) => {
        // A click on the backdrop lands on the dialog element itself; a click
        // on the content lands on a child. Only the first should dismiss.
        if (event.target === ref.current) onClose();
      }}
    >
      <div className="card modal-body">
        <button className="modal-close" type="button" onClick={onClose} aria-label="Close">
          ×
        </button>
        {children}
      </div>
    </dialog>
  );
}
