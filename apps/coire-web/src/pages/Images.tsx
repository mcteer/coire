import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelImageJob,
  deleteImageOutput,
  deleteImageHistory,
  downloadImageOutput,
  listImageJobs,
  listImageModels,
  listImageOutputs,
  listImagePresets,
  requestFromImageOutput,
  submitImageJob,
  type ImageJob,
  type ImageJobReceipt,
  type ImageModelList,
  type ImageOutput,
  type ImagePresetList,
  type ImageSubmitRequest,
} from "../api/images";
import { ApiError } from "../api/client";
import { ConfirmAction } from "../components/ConfirmAction";
import { ImageForm } from "../components/images/ImageForm";
import { ImageGallery } from "../components/images/ImageGallery";
import { ImageMetadataImport } from "../components/images/ImageMetadataImport";
import { ImageTimeline } from "../components/images/ImageTimeline";
import { PresetEditor } from "../components/images/PresetEditor";
import { isTerminalImageState, useImageJob } from "../hooks/useImageJob";

export function Images({
  canEditPresets = false,
  canGenerateExplicit = false,
}: {
  canEditPresets?: boolean;
  canGenerateExplicit?: boolean;
}) {
  const [historySelection, setHistorySelection] = useState<Set<string>>(new Set());
  const [deletingHistory, setDeletingHistory] = useState(false);
  const [showOlderJobs, setShowOlderJobs] = useState(false);
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
  const [receipt, setReceipt] = useState<ImageJobReceipt | null>(null);
  const [tag, setTag] = useState<ImageOutput["tag"] | "">("");
  const [reuse, setReuse] = useState<{ request: ImageSubmitRequest; revision: number } | null>(
    null,
  );
  useEffect(() => {
    const terminal = new Set(
      jobs.filter((job) => isTerminalImageState(job.state)).map((job) => job.id),
    );
    setHistorySelection((current) => {
      const next = new Set([...current].filter((id) => terminal.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [jobs]);
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
      setActiveJobId(
        (selected) =>
          selected ?? jobPage.items.find((job) => !isTerminalImageState(job.state))?.id ?? null,
      );
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
    if (submitting) return;
    setSubmitting(true);
    setError("");
    try {
      const receipt = await submitImageJob(request, crypto.randomUUID());
      setReceipt(receipt);
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
      throw cause;
    }
  };

  const deleteSelectedHistory = async () => {
    const ids = jobs
      .filter((job) => historySelection.has(job.id) && isTerminalImageState(job.state))
      .map((job) => job.id);
    setDeletingHistory(true);
    try {
      const result = await deleteImageHistory(ids);
      const deleted = new Set(result.deleted_job_ids);
      setJobs((current) => current.filter((job) => !deleted.has(job.id)));
      setHistorySelection(new Set());
      if (activeJobId && deleted.has(activeJobId)) {
        setActiveJobId(null);
        setReceipt(null);
      }
    } catch (cause) {
      noteFailure(cause);
    } finally {
      setDeletingHistory(false);
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
            canGenerateExplicit={canGenerateExplicit}
            presets={presets}
            feedback={
              submitting ? (
                <div className="image-submission-feedback" role="status">
                  Submitting your image request…
                </div>
              ) : activeJobId ? (
                <ImageTimeline
                  jobId={activeJobId}
                  state={active.state ?? receipt?.state ?? null}
                  step={active.step}
                  totalSteps={active.total}
                  queuePosition={active.queuePosition ?? receipt?.queue_position}
                  cacheStatus={active.cacheStatus}
                  workerResidency={active.workerResidency}
                  busy={busyJob === activeJobId}
                  error={active.error}
                  onStop={() => void stop(activeJobId)}
                  onViewResult={() => {
                    setTag("");
                    document.getElementById("image-library")?.scrollIntoView({ block: "start" });
                    document.getElementById("image-library")?.focus({ preventScroll: true });
                  }}
                />
              ) : null
            }
            disabled={submitting}
            onSubmit={generate}
            reuse={reuse}
          />
        )}
        <div className="image-workspace-tools">
          <details className="image-recipe-tools">
            <summary>Import image settings</summary>
            <ImageMetadataImport
              onImport={(request) =>
                setReuse((current) => ({ request, revision: (current?.revision ?? 0) + 1 }))
              }
            />
          </details>
          <button className="button image-refresh" type="button" onClick={() => void refresh()}>
            Refresh
          </button>
        </div>
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
      {!loading && (
        <details className="image-preset-tools">
          <summary>Manage presets</summary>
          <PresetEditor
            presets={presets}
            models={models}
            canEdit={canEditPresets}
            onChanged={refresh}
          />
        </details>
      )}
      <section className="panel glass wide" aria-label="Image jobs">
        <h2>Generation history</h2>
        <div className="image-history-selection">
          <p>Delete completed history and its events permanently. Library images remain.</p>
          <button
            className="button"
            type="button"
            disabled={deletingHistory || !jobs.some((job) => isTerminalImageState(job.state))}
            onClick={() =>
              setHistorySelection(
                new Set(jobs.filter((job) => isTerminalImageState(job.state)).map((job) => job.id)),
              )
            }
          >
            Select completed history
          </button>
          {historySelection.size > 0 && (
            <>
              <span role="status">{historySelection.size} selected</span>
              <button
                className="button"
                type="button"
                disabled={deletingHistory}
                onClick={() => setHistorySelection(new Set())}
              >
                Clear history selection
              </button>
              {!deletingHistory && (
                <ConfirmAction
                  label="Delete history"
                  target={`${historySelection.size} selected entries permanently`}
                  onConfirm={deleteSelectedHistory}
                />
              )}
            </>
          )}
          {deletingHistory && <span role="status">Deleting history…</span>}
        </div>
        {loading ? (
          <p>Loading image jobs…</p>
        ) : jobs.length === 0 ? (
          <p>No image jobs yet.</p>
        ) : null}
        {jobs.length > 0 && (
          <details className="image-job-history">
            <summary>
              Recent jobs <span>{jobs.length}</span>
            </summary>
            <ul className="image-job-list">
              {(showOlderJobs ? jobs : jobs.slice(0, 5)).map((job) => (
                <li key={job.id}>
                  <div className="image-job-description">
                    {isTerminalImageState(job.state) && (
                      <label className="image-history-check">
                        <input
                          type="checkbox"
                          aria-label={`Select history ${job.id}`}
                          checked={historySelection.has(job.id)}
                          disabled={deletingHistory}
                          onChange={(event) =>
                            setHistorySelection((current) => {
                              const next = new Set(current);
                              if (event.target.checked) next.add(job.id);
                              else next.delete(job.id);
                              return next;
                            })
                          }
                        />
                        Select entry
                      </label>
                    )}
                    <strong>{job.effective_spec?.prompt || "Image generation"}</strong>
                    <small>
                      <time dateTime={job.created_at}>
                        {new Date(job.created_at).toLocaleString()}
                      </time>{" "}
                      · {job.id.slice(-8)}
                    </small>
                    {job.failure_code && <small className="error">{job.failure_code}</small>}
                  </div>
                  <span className={`image-job-state state-${job.state}`}>
                    {
                      {
                        succeeded: "Complete",
                        failed: "Failed",
                        cancelled: "Stopped",
                        queued: "Queued",
                        reserving: "Preparing",
                        running: "Generating",
                        transferring: "Saving",
                        cancelling: "Stopping",
                      }[job.state]
                    }
                  </span>
                  <div className="image-job-actions">
                    {job.id !== activeJobId && (
                      <button
                        className="button"
                        type="button"
                        onClick={() => {
                          setReceipt({
                            job_id: job.id,
                            state: job.state,
                            queue_position: job.queue_position,
                          });
                          setActiveJobId(job.id);
                        }}
                        aria-label={`View progress for image job ${job.id}`}
                      >
                        View progress
                      </button>
                    )}
                    {!isTerminalImageState(job.state) &&
                      (job.id !== activeJobId || models.length === 0) && (
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
                  </div>
                </li>
              ))}
            </ul>
            {jobs.length > 5 && (
              <button
                className="button image-history-more"
                type="button"
                onClick={() => setShowOlderJobs((value) => !value)}
              >
                {showOlderJobs ? "Show fewer jobs" : "Show older jobs"}
              </button>
            )}
          </details>
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
