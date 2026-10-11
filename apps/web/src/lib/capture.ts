/**
 * Camera access, and turning what comes off it into the files the API already
 * takes.
 *
 * Everything here produces `File` objects and nothing else, because that is what
 * `api.uploadImages` wants and what a dropped folder produces. The capture page
 * and the drop zone therefore reach the server through the same call with the
 * same kind of argument, which is the only reason "all three work" means
 * something.
 *
 * **The two still paths are not equivalent, and it is not a small difference.**
 * A live camera gives the video track's resolution -- about 2 megapixels on iOS,
 * whatever the sensor is -- because that is what `getUserMedia` hands out.
 * `ImageCapture.takePhoto()` is the API that returns the sensor's full
 * resolution and Safari does not have it. The file input goes through the
 * system camera app instead, which has the real sensor, the real flash, and its
 * own idea of when to take the photograph.
 *
 * Which of those reconstructs better is a question for the evaluation harness,
 * not for intuition, so both are offered and neither is hidden.
 */

/** A camera that is open, with the knobs this device actually has. */
export interface CameraHandle {
  stream: MediaStream
  stop: () => void
  /** Whether the track advertises a torch. False on iOS, where it usually does not. */
  torch: boolean
  setTorch: (on: boolean) => Promise<void>
}

/** iOS needs `playsinline` or it takes the video fullscreen and never paints. */
export const VIDEO_ATTRIBUTES = {
  autoPlay: true,
  muted: true,
  playsInline: true,
} as const

export async function openCamera(): Promise<CameraHandle> {
  if (!navigator.mediaDevices?.getUserMedia) {
    // The overwhelmingly common cause, and not one the browser explains.
    throw new Error(
      'This browser will not give a page camera access. It has to be served over https, or from localhost.',
    )
  }

  const stream = await navigator.mediaDevices.getUserMedia({
    video: {
      // Ideal, not exact: a phone that cannot face that way ignores the hint,
      // while a constraint that cannot be met fails the whole request and the
      // user is left with a blank screen and no explanation.
      facingMode: { ideal: 'environment' },
      width: { ideal: 4096 },
    },
    audio: false,
  })

  const track = stream.getVideoTracks()[0]
  const capabilities = track.getCapabilities?.() as { torch?: boolean } | undefined
  const torch = Boolean(capabilities?.torch)

  return {
    stream,
    torch,
    stop: () => stream.getTracks().forEach((each) => each.stop()),
    setTorch: async (on: boolean) => {
      // `torch` is not in the standard constraint types yet, and never will be
      // for every browser -- this is the one place a cast is honest rather than
      // lazy. Doubled, because the literal is checked before the assertion.
      const advanced = [{ torch: on }] as unknown as MediaTrackConstraintSet[]
      await track.applyConstraints({ advanced })
    },
  }
}

export function pickRecorderType(): string | undefined {
  const candidates = ['video/webm;codecs=vp9', 'video/webm', 'video/mp4']
  return candidates.find((type) => MediaRecorder.isTypeSupported?.(type))
}

/**
 * One frame from a playing or paused video, as a JPEG.
 *
 * JPEG rather than PNG: these are photographs, and a lossless frame is several
 * times the size for no gain the reconstruction can use.
 */
export async function grabFrame(
  video: HTMLVideoElement,
  name: string,
  quality = 0.95,
): Promise<File> {
  const width = video.videoWidth
  const height = video.videoHeight
  if (!width || !height) {
    throw new Error('the camera has not produced a picture yet')
  }

  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const context = canvas.getContext('2d')
  if (!context) throw new Error('this browser would not give a drawing surface')

  context.drawImage(video, 0, 0, width, height)
  const blob = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, 'image/jpeg', quality),
  )
  if (!blob) throw new Error('the frame could not be encoded')

  return new File([blob], name, { type: 'image/jpeg' })
}

/**
 * Frames from a recording, evenly spaced.
 *
 * Evenly spaced rather than every nth frame: a recording of a slow arc holds a
 * great many near-identical frames, and picking them by position gives a set
 * spread across the whole sweep instead of a cluster wherever the hand paused.
 */
export async function extractFrames(
  recording: Blob,
  wanted: number,
  name: (index: number) => string,
): Promise<File[]> {
  const url = URL.createObjectURL(recording)
  const video = document.createElement('video')
  video.src = url
  video.muted = true
  video.playsInline = true

  try {
    await settled(video, 'loadedmetadata')
    const duration = video.duration
    if (!Number.isFinite(duration) || duration <= 0) {
      throw new Error('that recording has no length to take frames from')
    }

    const frames: File[] = []
    for (let index = 0; index < wanted; index += 1) {
      video.currentTime = (duration * (index + 0.5)) / wanted
      await settled(video, 'seeked')
      frames.push(await grabFrame(video, name(index)))
    }
    return frames
  } finally {
    URL.revokeObjectURL(url)
  }
}

function settled(element: HTMLMediaElement, event: string, timeout = 10_000): Promise<void> {
  return new Promise((resolve, reject) => {
    const done = () => {
      cleanup()
      resolve()
    }
    const failed = () => {
      cleanup()
      reject(new Error('that recording could not be read'))
    }
    const timer = window.setTimeout(failed, timeout)
    const cleanup = () => {
      window.clearTimeout(timer)
      element.removeEventListener(event, done)
      element.removeEventListener('error', failed)
    }
    element.addEventListener(event, done, { once: true })
    element.addEventListener('error', failed, { once: true })
  })
}
