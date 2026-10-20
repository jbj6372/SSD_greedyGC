# Greedy-GC SSD Write-Amplification Simulator

`greedy_gc_sim.py`는 논문의 page-level greedy garbage collection을 단순화해 구현한 Python 시뮬레이터다.

## 모델

- page-mapped, out-of-place update
- 물리 블록 수: `b = --blocks`
- 블록당 페이지 수: `c = --pages-per-block`
- 논리 용량: `u = --user-blocks`, 또는 `round(b × --occupancy)`
- 항상 clean block 1개를 GC용으로 예약
- 현재 write block이 가득 차면 GC 수행
- clean block을 제외하고 **valid page 수가 가장 적은 블록**을 victim으로 선택
- victim의 valid page를 clean block으로 복사한 뒤 victim을 erase
- 측정값:

```text
write_amplification = (host page writes + GC relocation page writes)
                      / host page writes
```

초기 preconditioning과 warm-up 구간의 쓰기는 측정에서 제외된다.

## 실행

의존성은 Python 3 표준 라이브러리뿐이다.

```bash
python greedy_gc_sim.py \
  --blocks 1000 \
  --pages-per-block 16 \
  --occupancy 0.8 \
  --workload uniform \
  --warmup-writes 500000 \
  --writes 1000000
```

stdout에는 write amplification 숫자 하나만 출력된다.

```text
2.37...
```

JSON 출력:

```bash
python greedy_gc_sim.py ... --output json
```

```json
{"write_amplification":2.37}
```

## 지원 workload

### 1. Uniform random single-page updates

논문의 기본 workload와 동일하다.

```bash
python greedy_gc_sim.py --workload uniform
```

### 2. Sequential cyclic writes

```bash
python greedy_gc_sim.py --workload sequential --start-lpn 0
```

### 3. Hot/cold

기본값은 전체 LPN의 10%가 전체 쓰기의 90%를 받는다.

```bash
python greedy_gc_sim.py \
  --workload hotcold \
  --hot-fraction 0.10 \
  --hot-write-ratio 0.90
```

### 4. Zipf

```bash
python greedy_gc_sim.py --workload zipf --zipf-alpha 1.2
```

`zipf`는 유한 LPN 범위에 대한 정확한 CDF를 생성하므로, 논리 페이지 수에 비례하는 초기 메모리와 시간이 든다.

### 5. Real trace

지원되는 자동 형식:

```text
lpn
W,lba,size
timestamp,W,lba,size
```

- `R`, `READ` 요청은 무시한다. 읽기는 write amplification에 영향을 주지 않는다.
- multi-page write는 page update 여러 개로 확장한다.
- 한 열짜리 trace는 항상 LPN 단위로 해석한다.
- 여러 열 trace의 LBA와 size 단위는 `--trace-unit`으로 지정한다.

예시:

```bash
python greedy_gc_sim.py \
  --blocks 1000 \
  --pages-per-block 128 \
  --occupancy 0.8 \
  --workload trace \
  --trace example_trace.csv \
  --trace-unit sector \
  --sector-size 512 \
  --page-size 4096 \
  --trace-repeat \
  --warmup-writes 500000 \
  --writes 1000000
```

MSR Cambridge 형태처럼 `timestamp,host,disk,Write,offset,size,...`인 경우, operation 뒤에 offset과 size가 오므로 자동 해석할 수 있다. 이 경우 offset과 size가 byte 단위라면:

```bash
python greedy_gc_sim.py ... \
  --workload trace \
  --trace msr.csv \
  --trace-unit byte
```

operation 열이 LBA/size 뒤에 있는 trace는 0-based column을 명시한다.

```bash
python greedy_gc_sim.py ... \
  --workload trace \
  --trace fiu.trace \
  --trace-delimiter ';' \
  --trace-op-col 5 \
  --trace-lba-col 3 \
  --trace-size-col 4 \
  --trace-unit sector
```

trace 주소가 논리 용량을 넘으면 기본적으로 오류를 낸다. 작은 실험용 장치에 trace를 접어 넣으려면 다음 옵션을 쓸 수 있다.

```bash
--trace-addressing wrap
```

원래 trace의 시작 LBA를 0으로 맞추려면:

```bash
--trace-lba-offset <minimum-LBA>
```

## 주요 옵션

```text
--blocks B                 물리 블록 수, 기본 1000
--pages-per-block C        블록당 페이지 수, 기본 128
--occupancy RHO            user-block/physical-block 비율
--user-blocks U            정확한 user block 수; occupancy보다 우선
--warmup-writes N          측정 전 버리는 host page writes
--writes N                 측정할 host page writes
--seed N                   재현 가능한 workload 및 GC tie-breaking
--gc-tie-break random      최소-valid victim 동률을 무작위 선택
--gc-tie-break lowest      최소 block ID 선택
--check-invariants         실행 마지막에 내부 상태를 검사
```

`--warmup-writes`를 생략하면 `2 × logical_pages`가 사용된다. hot/cold, Zipf, 짧은 반복 trace처럼 수렴이 느린 workload에서는 이 값을 늘리는 편이 좋다.

## 논문 수치 sanity check

논문의 예시와 같은 `b=10000`, `c=16`, `rho=0.8` uniform random workload는 충분히 warm-up하면 약 `2.36`의 write amplification을 낸다.

```bash
python greedy_gc_sim.py \
  --blocks 10000 \
  --pages-per-block 16 \
  --occupancy 0.8 \
  --workload uniform \
  --warmup-writes 1000000 \
  --writes 2000000 \
  --seed 7
```

한 실행의 출력 예:

```text
2.362827
```

## 범위

의도적으로 다음은 모델링하지 않는다.

- read latency 또는 write latency
- NAND channel/die 병렬성
- erase latency와 queueing
- wear leveling
- TRIM
- SLC cache
- mapping-cache miss
- program/erase failure

따라서 결과는 GC에 의해 발생한 **page-write amplification만** 나타낸다.
