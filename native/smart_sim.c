#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define NETSIM_API __declspec(dllexport)
#else
#define NETSIM_API
#endif

typedef struct {
    double time;
    int64_t seq;
    int kind;    /* 1 = arrival, 2 = reboot_start, 3 = reboot_end, 4 = retry */
    int target;
    int attempt;
} HeapEvent;

typedef struct {
    int num_nodes;
    int seed;
    double delay_min;
    double delay_max;
    double packet_loss;
    double reboot_at;
    double reboot_duration;
    int source_node;
    int smart_fanout; /* 0 = flood all healthy neighbors, >0 = max smart neighbors per wave */
    int max_retries;  /* 0 = no retry, >0 = adaptive backoff retries on drop */
} CSimConfig;

typedef struct {
    int seed;
    int total_nodes;
    int reached_count;
    double elapsed_time;
    int full_reach_achieved;
    double full_reach_time;
    int duplicates_suppressed;
    int packets_dropped;
    int retries_scheduled;
    int reboots_triggered;
    double smart_efficiency; /* fraction of neighbor deliveries avoided by smart fanout */
} CSimResult;

/* xorshift64* PRNG for fast, deterministic cross-platform simulation */
static uint64_t rng_next(uint64_t *state) {
    uint64_t x = *state;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    *state = x;
    return x * 0x2545F4914F6CDD1DULL;
}

static double rng_uniform(uint64_t *state, double low, double high) {
    if (high <= low) return low;
    double u = (double)(rng_next(state) >> 11) / 9007199254740992.0; /* [0, 1) */
    return low + (high - low) * u;
}

static void heap_push(HeapEvent *heap, int *size, int cap, HeapEvent ev) {
    if (*size >= cap) return;
    int idx = (*size)++;
    heap[idx] = ev;
    while (idx > 0) {
        int parent = (idx - 1) / 2;
        HeapEvent *p = &heap[parent];
        HeapEvent *c = &heap[idx];
        int c_before_p = (c->time < p->time) || (c->time == p->time && c->seq < p->seq);
        if (!c_before_p) break;
        HeapEvent tmp = *p;
        *p = *c;
        *c = tmp;
        idx = parent;
    }
}

static HeapEvent heap_pop(HeapEvent *heap, int *size) {
    HeapEvent top = heap[0];
    (*size)--;
    if (*size > 0) {
        heap[0] = heap[*size];
        int idx = 0;
        while (1) {
            int left = 2 * idx + 1;
            int right = 2 * idx + 2;
            int smallest = idx;
            if (left < *size) {
                if (heap[left].time < heap[smallest].time ||
                    (heap[left].time == heap[smallest].time && heap[left].seq < heap[smallest].seq)) {
                    smallest = left;
                }
            }
            if (right < *size) {
                if (heap[right].time < heap[smallest].time ||
                    (heap[right].time == heap[smallest].time && heap[right].seq < heap[smallest].seq)) {
                    smallest = right;
                }
            }
            if (smallest == idx) break;
            HeapEvent tmp = heap[idx];
            heap[idx] = heap[smallest];
            heap[smallest] = tmp;
            idx = smallest;
        }
    }
    return top;
}

/*
 * CSR (Compressed Sparse Row) graph representation:
 *   row_ptr[u] .. row_ptr[u+1] gives outgoing edges for node u
 *   col_idx[e] gives neighbor node v
 *   edge_cost[e] gives base latency / cost multiplier for edge e (>= 0.0001)
 *   disconnected_flags[n] is 1 if node n is offline, else 0
 *   out_reached[n] (optional) written with 1 if node n reached at end, else 0
 *   out_arrival_times[n] (optional) written with exact arrival time if reached, else -1.0
 */
NETSIM_API void netsim_run_smart_c(
    const CSimConfig *cfg,
    const int *row_ptr,
    const int *col_idx,
    const double *edge_cost,
    const int *disconnected_flags,
    CSimResult *out_res,
    int *out_reached,
    double *out_arrival_times
) {
    int n = cfg->num_nodes;
    memset(out_res, 0, sizeof(*out_res));
    out_res->seed = cfg->seed;
    out_res->total_nodes = n;
    out_res->full_reach_time = -1.0;

    if (n <= 0) return;

    int num_edges = row_ptr[n];
    int heap_cap = (num_edges + n) * (cfg->max_retries + 2) + 64;
    if (heap_cap < 256) heap_cap = 256;

    HeapEvent *heap = (HeapEvent *)malloc((size_t)heap_cap * sizeof(HeapEvent));
    int *state = (int *)malloc((size_t)n * sizeof(int));         /* 0=HEALTHY, 1=OFFLINE, 2=REBOOTING */
    int *reached = (int *)malloc((size_t)n * sizeof(int));
    double *arrival = (double *)malloc((size_t)n * sizeof(double));
    double *edge_penalty = (double *)malloc((size_t)(num_edges > 0 ? num_edges : 1) * sizeof(double));
    int *cand_edge = (int *)malloc((size_t)(n > 0 ? n : 1) * sizeof(int));
    double *cand_score = (double *)malloc((size_t)(n > 0 ? n : 1) * sizeof(double));

    for (int i = 0; i < n; i++) {
        state[i] = (disconnected_flags && disconnected_flags[i]) ? 1 : 0;
        reached[i] = 0;
        arrival[i] = -1.0;
    }
    for (int e = 0; e < num_edges; e++) {
        edge_penalty[e] = 1.0;
    }

    uint64_t rng = (uint64_t)(cfg->seed ? cfg->seed : 1) * 0x9E3779B97F4A7C15ULL;
    int heap_size = 0;
    int64_t seq = 0;
    double clock = 0.0;
    int reached_count = 0;
    int total_candidate_edges = 0;
    int total_selected_edges = 0;

    if (cfg->source_node >= 0 && cfg->source_node < n && state[cfg->source_node] == 0) {
        HeapEvent ev = {0.0, ++seq, 1, cfg->source_node, 0};
        heap_push(heap, &heap_size, heap_cap, ev);
    }
    if (cfg->reboot_at >= 0.0) {
        HeapEvent rev = {cfg->reboot_at, ++seq, 2, -1, 0};
        heap_push(heap, &heap_size, heap_cap, rev);
    }

    while (heap_size > 0) {
        HeapEvent ev = heap_pop(heap, &heap_size);
        clock = ev.time;

        if (ev.kind == 2) { /* reboot_start */
            out_res->reboots_triggered++;
            for (int i = 0; i < n; i++) {
                if (state[i] == 0) state[i] = 2;
                reached[i] = 0;
                arrival[i] = -1.0;
            }
            reached_count = 0;
            HeapEvent end_ev = {clock + cfg->reboot_duration, ++seq, 3, -1, 0};
            heap_push(heap, &heap_size, heap_cap, end_ev);
        } else if (ev.kind == 3) { /* reboot_end */
            for (int i = 0; i < n; i++) {
                if (state[i] == 2) state[i] = 0;
            }
            if (cfg->source_node >= 0 && cfg->source_node < n && state[cfg->source_node] == 0) {
                HeapEvent arr = {clock, ++seq, 1, cfg->source_node, 0};
                heap_push(heap, &heap_size, heap_cap, arr);
            }
        } else if (ev.kind == 1 || ev.kind == 4) { /* arrival or smart retry */
            int u = ev.target;
            if (u < 0 || u >= n || state[u] != 0) continue;

            if (ev.kind == 1) {
                if (reached[u]) {
                    out_res->duplicates_suppressed++;
                    continue;
                }
                reached[u] = 1;
                reached_count++;
                arrival[u] = clock;
            }

            int start_e = row_ptr[u];
            int end_e = row_ptr[u + 1];
            int cand_count = 0;

            for (int e = start_e; e < end_e; e++) {
                int v = col_idx[e];
                if (v < 0 || v >= n || state[v] != 0) continue;
                if (reached[v] && ev.kind == 1) continue; /* smart skip already-reached neighbors */
                double base_c = (edge_cost && edge_cost[e] > 0.0001) ? edge_cost[e] : rng_uniform(&rng, cfg->delay_min, cfg->delay_max);
                cand_edge[cand_count] = e;
                cand_score[cand_count] = base_c * edge_penalty[e];
                cand_count++;
            }

            total_candidate_edges += cand_count;

            /* Rank candidates by smart score (lowest latency * penalty first) */
            for (int i = 0; i < cand_count; i++) {
                for (int j = i + 1; j < cand_count; j++) {
                    if (cand_score[j] < cand_score[i]) {
                        double ts = cand_score[i];
                        cand_score[i] = cand_score[j];
                        cand_score[j] = ts;
                        int te = cand_edge[i];
                        cand_edge[i] = cand_edge[j];
                        cand_edge[j] = te;
                    }
                }
            }

            int limit = cand_count;
            if (cfg->smart_fanout > 0 && cand_count > cfg->smart_fanout) {
                limit = cfg->smart_fanout;
            }
            total_selected_edges += limit;

            for (int k = 0; k < limit; k++) {
                int e = cand_edge[k];
                int v = col_idx[e];
                double base_c = (edge_cost && edge_cost[e] > 0.0001) ? edge_cost[e] : rng_uniform(&rng, cfg->delay_min, cfg->delay_max);
                double jitter = rng_uniform(&rng, 0.85, 1.25);
                double backoff = (ev.attempt > 0) ? pow(1.5, (double)ev.attempt) : 1.0;
                double hop_delay = base_c * jitter * backoff;

                if (cfg->packet_loss > 0.0 && rng_uniform(&rng, 0.0, 1.0) < cfg->packet_loss) {
                    out_res->packets_dropped++;
                    if (edge_penalty[e] < 4.0) edge_penalty[e] *= 1.25;
                    if (ev.attempt < cfg->max_retries) {
                        out_res->retries_scheduled++;
                        HeapEvent retry_ev = {clock + hop_delay, ++seq, 4, v, ev.attempt + 1};
                        heap_push(heap, &heap_size, heap_cap, retry_ev);
                    }
                    continue;
                }

                /* Successful transmission slightly improves link confidence */
                if (edge_penalty[e] > 0.8) edge_penalty[e] *= 0.97;
                HeapEvent next_ev = {clock + hop_delay, ++seq, 1, v, 0};
                heap_push(heap, &heap_size, heap_cap, next_ev);
            }
        }
    }

    out_res->reached_count = reached_count;
    out_res->elapsed_time = clock;
    if (reached_count == n && n > 0) {
        out_res->full_reach_achieved = 1;
        double max_t = 0.0;
        for (int i = 0; i < n; i++) {
            if (arrival[i] > max_t) max_t = arrival[i];
        }
        out_res->full_reach_time = max_t;
    }

    if (out_reached) {
        for (int i = 0; i < n; i++) {
            out_reached[i] = reached[i];
        }
    }
    if (out_arrival_times) {
        for (int i = 0; i < n; i++) {
            out_arrival_times[i] = reached[i] ? arrival[i] : -1.0;
        }
    }

    if (total_candidate_edges > 0) {
        double saved = (double)(total_candidate_edges - total_selected_edges);
        out_res->smart_efficiency = (saved > 0.0) ? (saved / (double)total_candidate_edges) : 0.0;
    }

    free(heap);
    free(state);
    free(reached);
    free(arrival);
    free(edge_penalty);
    free(cand_edge);
    free(cand_score);
}