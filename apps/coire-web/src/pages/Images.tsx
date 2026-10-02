import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelImageJob,
  deleteImageOutput,
  downloadImageOutput,
  listImageJobs,
  listImageModels,
  listImageOutputs,
  listImagePresets,
  requestFromImageOutput,
  submitImageJob,
  type ImageJob,
  type ImageModelList,
  type ImageOutput,
  type ImagePresetList,
  type ImageSubmitRequest,
} from "../api/images";
import { ApiError } from "../api/client";
import { ImageForm } from "../components/images/ImageForm";
import { ImageGallery } from "../components/images/ImageGallery";
import { ImageMetadataImport } from "../components/images/ImageMetadataImport";
import { ImageTimeline } from "../components/images/ImageTimeline";
import { PresetEditor } from "../components/images/PresetEditor";
import { isTerminalImageState, useImageJob } from "../hooks/useImageJob";

export function Images({ canEditPresets = false }: { canEditPresets?: boolean }) {
  const [jobs, setJobs] = useState<ImageJob[]>([]);
  const [outputs, setOutputs] = useState<ImageOutput[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [authExpired, setAuthExpired] = useState(false);
  const [busyJob, setBusyJob] = useState<string | null>(null);
  const [busyOutput, setBusyOutput] = useState<string | null>(null);
  const [models, setModels] = useState<ImageModelList["items"]>([]);
  const [limits, setLimits] = useState<ImageModelList["limits"]>(null);
  const [presets, setPresets] = useState<ImagePresetList["items"]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [tag, setTag] = useState<ImageOutput["tag"] | "">("");
  const [reuse, setReuse] = useState<{ request: ImageSubmitRequest; revision: number } | null>(
    null,
  );
  const active = useImageJob(activeJobId);
  const refreshedTerminalJob = useRef<string | null>(null);

  const noteFailure = (cause: unknown) => {
    if (cause instanceof ApiError && cause.status === 401) setAuthExpired(true);
    setError(String(cause));
  };

  useEffect(() => {
    if (active.error?.includes("stream refused (401)")) setAuthExpired(true);
  }, [active.error]);

  const refresh = useCallback(async () => {
    setError("");
    try {
      const [jobPage, outputPage, modelPage, presetPage] = await Promise.all([
        listImageJobs(),
        listImageOutputs(null, 25, tag || undefined),
        listImageModels(),
        listImagePresets(),
      ]);
      setJobs(jobPage.items);
      setOutputs(outputPage.items);
      setModels(modelPage.items);
      setLimits(modelPage.limits ?? null);
      setPresets(presetPage.items);
      setNextCursor(outputPage.next_cursor ?? null);
      setAuthExpired(false);
    } catch (cause) {
      setLimits(null);
      noteFailure(cause);
    } finally {
      setLoading(false);
    }
  }, [tag]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (
      !activeJobId ||
      active.observedJobId !== activeJobId ||
      !active.terminal ||
      refreshedTerminalJob.current === activeJobId
    )
      return;
    refreshedTerminalJob.current = activeJobId;
    // Read the committed job and gallery after the terminal server event; a job receipt
    // alone never makes partial outputs visible.
    void refresh();
  }, [activeJobId, active.observedJobId, active.terminal, refresh]);

  const generate = async (request: ImageSubmitRequest) => {
    setSubmitting(true);
    setError("");
    try {
      const receipt = await submitImageJob(request, crypto.randomUUID());
      setActiveJobId(receipt.job_id);
      await refresh();
    } catch (cause) {
      noteFailure(cause);
    } finally {
      setSubmitting(false);
    }
  };

  const stop = async (jobId: string) => {
    setBusyJob(jobId);
    setError("");
    try {
      await cancelImageJob(jobId);
      await refresh();
    } catch (cause) {
      noteFailure(cause);
    } finally {
      setBusyJob(null);
    }
  };

  const older = async () => {
    if (!nextCursor) return;
    setError("");
    try {
      const page = await listImageOutputs(nextCursor, 25, tag || undefined);
      setOutputs((current) => [...current, ...page.items]);
      setNextCursor(page.next_cursor ?? null);
    } catch (cause) {
      noteFailure(cause);
    }
  };

  const download = async (output: ImageOutput) => {
    setBusyOutput(output.id);
    setError("");
    try {
      const blob = await downloadImageOutput(output.id);
      const url = URL.createObjectURL(blob);
      try {
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = `coire-${output.id}.png`;
        document.body.append(anchor);
        try {
          anchor.click();
        } finally {
          anchor.remove();
        }
      } finally {
        window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
      }
    } catch (cause) {
      noteFailure(cause);
    } finally {
      setBusyOutput(null);
    }
  };

  const remove = async (outputId: string) => {
    setError("");
    try {
      await deleteImageOutput(outputId);
      setOutputs((current) => current.filter((item) => item.id !== outputId));
    } catch (cause) {
      noteFailure(cause);
    }
  };

  return (
    <main className="grid image-page" aria-label="Images">
      <section className="panel glass wide">
        <h1>Images</h1>
        {models.length === 0 ? (
          <p>Generation is unavailable until an image model is published.</p>
        ) : (
          <ImageForm
            models={models}
            limits={limits}
            presets={presets}
            disabled={submitting}
            onSubmit={generate}
            reuse={reuse}
          />
        )}
        <ImageMetadataImport
          onImport={(request) =>
            setReuse((current) => ({ request, revision: (current?.revision ?? 0) + 1 }))
          }
        />
        <button className="button" type="button" onClick={() => void refresh()}>
          Refresh
        </button>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {authExpired && (
          <a className="button" href="/">
            Sign in again
          </a>
        )}
      </section>
      {activeJobId && (
        <ImageTimeline
          jobId={activeJobId}
          state={active.state}
          step={active.step}
          totalSteps={active.total}
          cacheStatus={active.cacheStatus}
          workerResidency={active.workerResidency}
          busy={busyJob === activeJobId}
          error={active.error}
          onStop={() => void stop(activeJobId)}
        />
      )}
      {!loading && (
        <PresetEditor
          presets={presets}
          models={models}
          canEdit={canEditPresets}
          onChanged={refresh}
        />
      )}
      <section className="panel glass wide" aria-label="Image jobs">
        <h2>Jobs</h2>
        {loading ? (
          <p>Loading image jobs…</p>
        ) : jobs.length === 0 ? (
          <p>No image jobs yet.</p>
        ) : null}
        {jobs.length > 0 && (
          <ul>
            {jobs.map((job) => (
              <li key={job.id}>
                <span className="mono">{job.id}</span> · {job.state}
                {job.failure_code && <> · {job.failure_code}</>}
                {!isTerminalImageState(job.state) && job.id !== activeJobId && (
                  <button
                    className="button"
                    type="button"
                    disabled={busyJob === job.id}
                    onClick={() => void stop(job.id)}
                    aria-label={`Stop image job ${job.id}`}
                  >
                    {busyJob === job.id ? "Stopping…" : "Stop"}
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
      <ImageGallery
        outputs={outputs}
        loading={loading}
        tag={tag}
        onTagChange={setTag}
        nextCursor={nextCursor}
        busyId={busyOutput}
        onDownload={(output) => void download(output)}
        onDelete={remove}
        onLoadOlder={() => void older()}
        onReuse={(output) =>
          setReuse((current) => ({
            request: requestFromImageOutput(output),
            revision: (current?.revision ?? 0) + 1,
          }))
        }
        onRegenerate={(output, newSeed) => void generate(requestFromImageOutput(output, newSeed))}
      />
    </main>
  );
}
