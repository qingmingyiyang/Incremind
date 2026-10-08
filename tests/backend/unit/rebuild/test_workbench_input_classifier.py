from core.product_core import (
    ClassifyWorkbenchInput,
    EnhanceWorkbenchInputClassification,
    ServeWorkbenchInputClassifierEndpoint,
)


def test_classifier_routes_plain_idea_to_text_inspiration_workflow():
    result = ClassifyWorkbenchInput().execute(content="灵感：资料库总览可以显示年度热力图")

    assert result.input_type == "direct_idea"
    assert result.intent == "inspiration"
    assert result.target_intake == "text_source_intake"
    assert result.auto_workflow == "text_auto_organization"
    assert result.provider_boundary == "local_rule_classifier_no_remote_provider"
    assert "save_original_text" in result.workflow_steps


def test_classifier_routes_webpage_and_video_links_separately():
    classifier = ClassifyWorkbenchInput()

    webpage = classifier.execute(content="https://example.com/article")
    video = classifier.execute(content="https://www.bilibili.com/video/BV1234567890")

    assert webpage.input_type == "webpage"
    assert webpage.target_intake == "link_source_intake"
    assert webpage.auto_workflow == "link_auto_organization"
    assert video.input_type == "video"
    assert video.route == "video_link_workflow"
    assert video.auto_workflow == "video_auto_workflow"
    assert "extract_audio" in video.workflow_steps


def test_classifier_splits_multiple_links_and_recommends_main_provider():
    result = ClassifyWorkbenchInput().execute(
        content="https://example.com/a https://www.bilibili.com/video/BV1234567890"
    )

    assert result.input_type == "bookmark_collection"
    assert result.provider_enhancement_recommended is True
    assert result.recommended_provider_role == "intake_main_model"
    assert len(result.child_inputs) == 2
    assert result.child_inputs[0]["input_type"] == "webpage"
    assert result.child_inputs[1]["input_type"] == "video"


def test_classifier_routes_files_to_required_local_capabilities():
    classifier = ClassifyWorkbenchInput()

    pdf = classifier.execute(media_type="application/pdf", file_name="产品设计.pdf")
    meeting = classifier.execute(media_type="audio/wav", file_name="会议录音.wav")

    assert pdf.input_type == "pdf"
    assert pdf.media_required_capability == "document_text_extraction"
    assert pdf.target_intake == "file_source_intake"
    assert meeting.input_type == "meeting_recording"
    assert meeting.media_required_capability == "asr"
    assert meeting.target_intake == "audio_source_intake"


def test_classifier_endpoint_serializes_privacy_boundary_without_provider_payload():
    response = ServeWorkbenchInputClassifierEndpoint().execute(
        method="POST",
        path="/api/rebuild/workbench/input-classifier",
        body={"content": "下一步实现前置分类器"},
        classify_input=ClassifyWorkbenchInput().execute,
    )

    assert response.status_code == 200
    assert response.body["status"] == "classified"
    assert response.body["intent"] == "project_progress"
    assert response.body["provider_boundary"] == "local_rule_classifier_no_remote_provider"
    assert response.body["recommended_provider_role"] == "lightweight_task_model"
    assert "privacy_boundary" in response.body
    assert "api_key" not in response.body
    assert "cookie" not in response.body
    assert response.body["classifier_prompt_source"] == "product_core_default"


def test_classifier_accepts_developer_studio_prompt_metadata_without_returning_prompt_content():
    result = ClassifyWorkbenchInput().execute(
        content="https://example.com/a",
        classifier_prompt={
            "id": "pt-input-understanding",
            "revision": 7,
            "source": "developer_studio",
            "content": "自定义输入理解提示词：低置信度才提示确认。",
        },
    )

    payload = result.__dict__ if hasattr(result, "__dict__") else {}
    serialized = ServeWorkbenchInputClassifierEndpoint().execute(
        method="POST",
        path="/api/rebuild/workbench/input-classifier",
        body={"content": "https://example.com/a"},
        classify_input=ClassifyWorkbenchInput().execute,
        classifier_prompt={
            "id": "pt-input-understanding",
            "revision": 7,
            "source": "developer_studio",
            "content": "自定义输入理解提示词：低置信度才提示确认。",
        },
    ).body

    assert result.classifier_prompt_id == "pt-input-understanding"
    assert result.classifier_prompt_revision == 7
    assert result.classifier_prompt_source == "developer_studio"
    assert serialized["classifier_prompt_revision"] == 7
    assert "自定义输入理解提示词" not in str(serialized)
    assert "content" not in payload


class FakeClassifierProvider:
    def complete_json(self, *, system_prompt, user_payload):
        assert "前置输入分类器" in system_prompt
        assert "低置信度才提示确认" in system_prompt
        assert "api_key" not in str(user_payload).lower()
        assert "cookie" not in str(user_payload).lower()
        return {
            "input_type": "bookmark_collection",
            "intent": "knowledge_supplement",
            "route": "multi_link_provider_routed",
            "confidence": 0.94,
            "workflow_steps": ["save_original_links", "read_web_pages", "structure_each_item"],
            "child_inputs": [
                {
                    "input_type": "webpage",
                    "intent": "knowledge_supplement",
                    "route": "webpage_intake",
                    "raw_input": "https://example.com/a",
                },
                {
                    "input_type": "video",
                    "intent": "knowledge_supplement",
                    "route": "video_link_workflow",
                    "raw_input": "https://www.bilibili.com/video/BV1234567890",
                },
            ],
            "reason": "Provider split links and confirmed workflows.",
        }


def test_provider_enhancer_merges_provider_child_inputs_without_secret_material():
    local = ClassifyWorkbenchInput().execute(
        content="https://example.com/a https://www.bilibili.com/video/BV1234567890"
    )

    result = EnhanceWorkbenchInputClassification().execute(
        local_result=local,
        provider=FakeClassifierProvider(),
        provider_name="intake-main-model",
        content="https://example.com/a cookie=secret https://www.bilibili.com/video/BV1234567890",
        classifier_prompt={
            "id": "pt-input-understanding",
            "revision": 5,
            "source": "developer_studio",
            "content": "低置信度才提示确认。",
        },
    )

    assert result.status == "provider_enhanced"
    assert result.provider_boundary == "provider_enhanced_by:intake-main-model"
    assert result.route == "multi_link_provider_routed"
    assert result.provider_enhancement_recommended is False
    assert len(result.child_inputs) == 2
    assert result.child_inputs[1]["input_type"] == "video"
    assert result.classifier_prompt_revision == 5


def test_endpoint_keeps_local_classification_when_provider_enhancement_fails():
    def fail_enhancement(**_kwargs):
        raise ValueError("provider API key is not configured")

    response = ServeWorkbenchInputClassifierEndpoint().execute(
        method="POST",
        path="/api/rebuild/workbench/input-classifier",
        body={
            "content": "https://example.com/a https://www.bilibili.com/video/BV1234567890",
            "allow_provider_enhancement": True,
        },
        classify_input=ClassifyWorkbenchInput().execute,
        enhance_input=fail_enhancement,
    )

    assert response.status_code == 200
    assert response.body["status"] == "classified"
    assert response.body["provider_enhancement_recommended"] is True
    assert len(response.body["child_inputs"]) == 2
