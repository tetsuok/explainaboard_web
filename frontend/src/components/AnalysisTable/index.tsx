import React, { useEffect, useState } from "react";
import { Table, Typography } from "antd";
import { ColumnsType } from "antd/lib/table";
import { SystemOutput } from "../../clients/openapi";
import { backendClient } from "../../clients";
import { PageState } from "../../utils";
import { SystemAnalysisParsed } from "../AnalysisReport/types";

interface Props {
  systemID: string;
  task: string;
  // The latter type is for NER
  outputIDs: string[] | { [key: string]: string }[];
  featureKeyToDescription: SystemAnalysisParsed["featureKeyToDescription"];
  page: number;
  setPage: React.Dispatch<React.SetStateAction<number>>;
}

export function AnalysisTable({
  systemID,
  task,
  outputIDs,
  featureKeyToDescription,
  page,
  setPage,
}: Props) {
  const [pageState, setPageState] = useState(PageState.loading);
  const [systemOutputs, setSystemOutputs] = useState<SystemOutput[]>([]);
  const pageSize = 10;
  const total = outputIDs.length;
  const offset = page * pageSize;
  const end = Math.min(offset + pageSize, outputIDs.length);
  const outputIDString = outputIDs.slice(offset, end).join(",");

  useEffect(() => {
    async function refreshSystemOutputs() {
      setPageState(PageState.loading);
      const result = await backendClient.systemsSystemIdOutputsGet(
        systemID,
        outputIDString
      );
      setSystemOutputs(result.system_outputs);
      setPageState(PageState.success);
    }
    refreshSystemOutputs();
  }, [systemID, outputIDString, page, pageSize]);

  const columns: ColumnsType<SystemOutput> = [];
  let dataSource;

  if (systemOutputs.length == 0) {
    return null;
  }

  // ID
  columns.push({
    dataIndex: "id",
    title: "ID",
    render: (value) => (
      <Typography.Paragraph copyable style={{ marginBottom: 0 }}>
        {value}
      </Typography.Paragraph>
    ),
  });

  // task NER
  if (task === "named-entity-recognition") {
    for (const subFeatureKey of Object.keys(outputIDs[0])) {
      columns.push({
        dataIndex: subFeatureKey,
        title: subFeatureKey,
        ellipsis: true,
      });
    }
    // TODO API request to retrieve text
    dataSource = outputIDs.map(function (o) {
      const output = o as { [key: string]: string };
      return { ...output };
    });
  } else {
    // other tasks
    const systemOutputFirst = systemOutputs[0];
    for (const systemOutputKey of Object.keys(systemOutputFirst)) {
      if (systemOutputKey === "id") {
        continue;
      }
      columns.push({
        dataIndex: systemOutputKey,
        title: featureKeyToDescription[systemOutputKey] || systemOutputKey,
        ellipsis: true,
      });
    }
    // clone the system output for modification
    dataSource = systemOutputs.map(function (systemOutput) {
      const processedSystemOutput = { ...systemOutput };
      for (const [key, output] of Object.entries(systemOutput)) {
        /*Task like QA have object output besides string and number.
          For now we serialize the object to a displayable string.
          TODO: unnest or use a nested table
        */
        if (typeof output === "object") {
          const processedOutput = Object.entries(output)
            .map(([subKey, subOutput]) => {
              return `${subKey}: ${subOutput}`;
            })
            .join("\n");
          processedSystemOutput[key] = processedOutput;
        }
      }
      return processedSystemOutput;
    });
  }

  return (
    <Table
      className="table"
      columns={columns}
      dataSource={dataSource}
      loading={pageState === PageState.loading}
      rowKey="id"
      pagination={{
        total,
        showTotal: (total, range) =>
          `${range[0]}-${range[1]} (total: ${total})`,
        pageSize,
        // conversion between 0-based and 1-based index
        current: page + 1,
        onChange: (newPage, newPageSize) => {
          setPage(newPage - 1);
        },
      }}
      scroll={{ y: 400 }}
    />
  );
}
