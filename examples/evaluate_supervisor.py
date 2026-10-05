"""Run synthetic Chinese incidents against a configured endpoint; never save credentials."""
import argparse
import asyncio
import json
from pathlib import Path

from local_ai_connector.supervisor import advise
from local_ai_connector.core import ConnectorError
import httpx


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--data",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    config=json.loads((args.data/"model.json").read_text())
    cases=json.loads((Path(__file__).resolve().parents[1]/"tests/model_cases.json").read_text())
    results=[]
    for case in cases:
        event={k:v for k,v in case.items() if k!="expected"}
        try:
            result=await advise(config,event)
            result.update(event=event,expected=case["expected"],accepted=result["advice"]["suggestion"] in case["expected"])
        except (ConnectorError,httpx.RequestError) as exc:
            result={"event":event,"accepted":False,"error":str(exc)}
        results.append(result)
        print(case["code"],result["accepted"],flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({"model":config["model"],"cases":results,"accepted":sum(r["accepted"] for r in results),"total":len(results)},ensure_ascii=False,indent=2))


asyncio.run(main())
