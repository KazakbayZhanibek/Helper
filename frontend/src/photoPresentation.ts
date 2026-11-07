/** Keep the frame description available to assistive technology. */
export function photoAlternative(label: string, caption?: string): string {
  const description = caption?.trim();
  return description ? `${label}: ${description}` : label;
}

/** A manual edit may change the meaning of an AI-selected frame. */
export function editedPhotoWarning(origin: string | undefined): boolean {
  return origin === "ai_edited";
}

// A linguistic cue is only a reason to look again, not proof of an error.
// Keep original text and photos unchanged, including in saved publications.
export function needsIntentReview(...texts: Array<string | undefined>): boolean {
  const cue = /(?:^|[^\p{L}])(?:чтобы|целью|үшін|so\s+that|in\s+order\s+to)(?=$|[^\p{L}])|(?:^|[^\p{L}])идеальн\p{L}*\s+(?:гладк\p{L}*|ровн\p{L}*|однородн\p{L}*)(?=$|[^\p{L}])/iu;
  return texts.some((text) => typeof text === "string" && cue.test(text));
}
