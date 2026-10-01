"""The looks of the AI videos. Each one is an engineered prompt: how every picture is drawn (look), how it
moves (motion), and what the video model must avoid. The same words go into every picture and every shot of
a story, so all its scenes look like one film.

The prompts describe a look instead of naming studios or artists: Google's models refuse or water down
"in the style of <studio>", and a described look is ours to use.
"""

from __future__ import annotations

from dataclasses import dataclass

COMPOSITION = ("Vertical 9:16 frame. Keep the faces and the main action in the upper two thirds of the frame and leave "
               "the lower third calm and simple, because captions go there.")
NO_TEXT = ("The picture has no text anywhere: no letters, words, numbers, captions, signs with writing, logos or "
           "watermarks.")
VIDEO_AVOID = ("text, subtitles, captions, letters, words, watermark, logo, talking, lip movement, singing, music, "
               "morphing, flickering, distorted faces, extra fingers, extra limbs, sudden cuts")


@dataclass(frozen=True)
class Style:
    key: str
    name: str
    about: str  # shown in the menu and on the phone
    look: str  # how every picture is drawn
    motion: str  # how every shot moves
    avoid: str  # what the video model must not do (its negative prompt)

    def picture_prompt(self, scene: str, setting: str, cast: str, camera: str) -> str:
        """One keyframe: the scene as a film still in this look."""
        parts = [f"A film still from a short animated story. {scene.strip()}"]
        if cast:
            parts.append(cast)
        if setting:
            parts.append(f"The world of the story: {setting.strip()}")
        if camera:
            parts.append(f"Framing: {camera.strip()}.")
        parts += [f"The look: {self.look}", COMPOSITION, NO_TEXT]
        return "\n\n".join(parts)

    def character_prompt(self, name: str, look: str) -> str:
        """The reference picture of one character, used for every scene they are in."""
        return "\n\n".join([
            f"A character design reference picture of {name}: {look.strip()}",
            "Full body, standing in a relaxed neutral pose and turned slightly toward the viewer, the whole figure "
            "visible from head to feet, on a plain soft light-grey background with nothing else in the picture.",
            f"The look: {self.look}",
            NO_TEXT,
        ])

    def video_prompt(self, scene: str, camera: str, sound: str) -> str:
        """One shot, animated from its keyframe."""
        parts = [scene.strip()]
        if camera:
            parts.append(f"Camera: {camera.strip()}.")
        parts.append(self.motion)
        parts.append(f"Sound: {sound.strip() if sound else 'quiet ambient sound of the place'}. No music, no speech, "
                     "no voices, nobody talks.")
        return " ".join(parts)


STYLES: dict[str, Style] = {
    "claymation": Style(
        key="claymation",
        name="Claymation",
        about="handmade clay stop-motion, cozy and charming",
        look=(
            "Handmade stop-motion claymation. Every character and object is sculpted from matte plasticine with visible "
            "fingerprints, tool marks and slightly uneven hand-shaped edges. Characters have rounded, chunky proportions, "
            "oversized expressive eyes, simple felt and knitted clothing, and faces that read clearly from a distance. The "
            "set is a miniature built on a tabletop from clay, cardboard, wood, felt and painted backdrops, full of tiny "
            "handmade props. Soft, warm studio lighting with a gentle key light, soft shadows and a little rim light; a "
            "shallow depth of field like a macro lens, so the background melts into a creamy blur. Rich, earthy colors "
            "with a few saturated accents. Charming, tactile and a little whimsical, like a feature made by a small "
            "stop-motion studio."
        ),
        motion=(
            "Stop-motion claymation animation: slightly stepped, handmade movement as if animated frame by frame, with "
            "tiny wobbles in the clay between frames; the characters move with weight and clear, simple gestures; the "
            "camera glides smoothly on a miniature dolly."
        ),
        avoid=f"photorealistic, live action, smooth glossy CGI, anime, {VIDEO_AVOID}",
    ),
    "anime": Style(
        key="anime",
        name="Anime",
        about="cinematic Japanese anime, painted skies and dramatic light",
        look=(
            "A modern Japanese anime feature film frame. Clean, confident line art and cel shading with soft gradient "
            "shadows; characters with expressive eyes, detailed hair that catches the light and natural proportions. "
            "Lush hand-painted backgrounds with atmospheric perspective, detailed clouds and glowing skies. Dramatic "
            "cinematic lighting: golden hour or moonlight, glowing rim light, soft bloom, light shafts and drifting "
            "particles in the air. Vibrant but harmonious colors with deep blues and warm highlights. Emotional, epic and "
            "beautifully composed, with the polish of a big-screen anime release."
        ),
        motion=(
            "Anime animation: expressive character acting with hair and clothes moving in the wind, drifting particles "
            "and light flickering in the air, gentle parallax between the painted background layers; the camera moves "
            "slowly and cinematically."
        ),
        avoid=f"photorealistic, live action, 3D render, clay, western cartoon, {VIDEO_AVOID}",
    ),
    "cartoon3d": Style(
        key="cartoon3d",
        name="3D cartoon",
        about="polished 3D animated movie, bright and expressive",
        look=(
            "A frame from a polished 3D animated feature film. Appealing stylized characters with exaggerated proportions, "
            "big expressive eyes, soft rounded shapes and lively poses; skin with soft subsurface scattering, detailed hair "
            "and fabric. Richly detailed, slightly stylized environments. Global illumination with a warm key light, a "
            "cool fill light and soft shadows; cinematic depth of field and gentle volumetric light. A vibrant, saturated "
            "and harmonious color palette. Playful, emotional and high-end, like a big family animation release."
        ),
        motion=(
            "3D feature animation: fluid, bouncy and expressive character animation with squash and stretch, readable "
            "gestures and facial expressions, hair and cloth reacting to movement; the camera moves smoothly like a "
            "film crane or dolly."
        ),
        avoid=f"photorealistic, live action, 2D, anime, clay, low-poly video game graphics, {VIDEO_AVOID}",
    ),
}
DEFAULT_STYLE = "claymation"


def get(key: str) -> Style:
    return STYLES.get((key or "").strip().lower(), STYLES[DEFAULT_STYLE])
