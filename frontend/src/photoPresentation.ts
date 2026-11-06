/** Keep the frame description available to assistive technology. */
export function photoAlternative(label: string, caption?: string): string {
  const description = caption?.trim();
  return description ? `${label}: ${description}` : label;
}

/** A manual edit may change the meaning of an AI-selected frame. */
export function editedPhotoWarning(origin: string | undefined): boolean {
  return origin === "ai_edited";
}
