from rest_framework import viewsets
from rest_framework import mixins

from apps.endpoints.models import Endpoint
from apps.endpoints.serializers import EndpointSerializer

from apps.endpoints.models import MLAlgorithm
from apps.endpoints.serializers import MLAlgorithmSerializer

from apps.endpoints.models import MLAlgorithmStatus
from apps.endpoints.serializers import MLAlgorithmStatusSerializer

from apps.endpoints.models import MLRequest
from apps.endpoints.serializers import MLRequestSerializer

from django.db import transaction
from apps.endpoints.models import ABTest
from apps.endpoints.serializers import ABTestSerializer

import json
from numpy.random import rand
import datetime

from rest_framework import views, status
from rest_framework.response import Response
from apps.ml.registry import MLRegistry
from server.wsgi import registry


class EndpointViewSet(
    mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    serializer_class = EndpointSerializer
    queryset = Endpoint.objects.all()


class MLAlgorithmViewSet(
    mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    serializer_class = MLAlgorithmSerializer
    queryset = MLAlgorithm.objects.all()


def deactivate_other_statuses(instance):
    old_statuses = MLAlgorithmStatus.objects.filter(parent_mlalgorithm=instance.parent_mlalgorithm,
                                                    created_at__lt=instance.created_at,
                                                    active=True)
    for i in range(len(old_statuses)):
        old_statuses[i].active = False
    MLAlgorithmStatus.objects.bulk_update(old_statuses, ["active"])


class MLAlgorithmStatusViewSet(
    mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet,
    mixins.CreateModelMixin
):
    serializer_class = MLAlgorithmStatusSerializer
    queryset = MLAlgorithmStatus.objects.all()

    def perform_create(self, serializer):
        try:
            with transaction.atomic():
                instance = serializer.save(active=True)
                # set active=False for other statuses
                deactivate_other_statuses(instance)
        except Exception as e:
            raise APIException(str(e))


class MLRequestViewSet(
    mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet,
    mixins.UpdateModelMixin
):
    serializer_class = MLRequestSerializer
    queryset = MLRequest.objects.all()


class RecommendView(views.APIView):
    def post(self, request, endpoint_name, format=None):

        algorithm_status = self.request.query_params.get("status", "production")
        algorithm_version = self.request.query_params.get("version")

        algs = MLAlgorithm.objects.filter(parent_endpoint__name=endpoint_name,
                                          status__status=algorithm_status,
                                          status__active=True)

        if algorithm_version is not None:
            algs = algs.filter(version=algorithm_version)

        if len(algs) == 0:
            return Response(
                {"status": "Error", "message": "ML algorithm is not available"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(algs) != 1 and algorithm_status != "ab_testing":
            return Response(
                {"status": "Error",
                 "message": "ML algorithm selection is ambiguous. Please specify algorithm version."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        alg_index = 0
        if algorithm_status == "ab_testing":
            alg_index = 0 if rand() < 0.5 else 1

        algorithm_object = registry.endpoints[algs[alg_index].id]
        recommendation = algorithm_object.get_recommendation(request.data)

        titles = recommendation["titles"] if "titles" in recommendation else "error"
        ml_request = MLRequest(
            input_data=json.dumps(request.data),
            full_response=recommendation,
            response=titles,
            feedback="",
            parent_mlalgorithm=algs[alg_index],
        )
        ml_request.save()

        recommendation["request_id"] = ml_request.id

        return Response(recommendation)


class ABTestViewSet(
    mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet,
    mixins.CreateModelMixin, mixins.UpdateModelMixin
):
    serializer_class = ABTestSerializer
    queryset = ABTest.objects.all()

    def perform_create(self, serializer):
        try:
            with transaction.atomic():
                instance = serializer.save()

                # update status for first algorithm
                status_1 = MLAlgorithmStatus(status="ab_testing",
                                             created_by=instance.created_by,
                                             parent_mlalgorithm=instance.parent_mlalgorithm_1,
                                             active=True)
                status_1.save()
                deactivate_other_statuses(status_1)

                # update status for second algorithm
                status_2 = MLAlgorithmStatus(status="ab_testing",
                                             created_by=instance.created_by,
                                             parent_mlalgorithm=instance.parent_mlalgorithm_2,
                                             active=True)
                status_2.save()
                deactivate_other_statuses(status_2)

        except Exception as e:
            raise APIException(str(e))


class StopABTestView(views.APIView):
    def post(self, request, ab_test_id, format=None):

        try:
            ab_test = ABTest.objects.get(pk=ab_test_id)

            if ab_test.ended_at is not None:
                return Response({"message": "AB Test already finished."})

            date_now = datetime.datetime.now()

            # Algorithm 1 output
            all_responses_1 = MLRequest.objects.filter(parent_mlalgorithm=ab_test.parent_mlalgorithm_1,
                                                       created_at__gt=ab_test.created_at,
                                                       created_at__lt=date_now)
            print(all_responses_1)

            # Algorithm 2 output
            all_responses_2 = MLRequest.objects.filter(parent_mlalgorithm=ab_test.parent_mlalgorithm_2,
                                                       created_at__gt=ab_test.created_at,
                                                       created_at__lt=date_now)
            print(all_responses_2)

            # get the algorithm IDs
            alg_id_1, alg_id_2 = ab_test.parent_mlalgorithm_1, ab_test.parent_mlalgorithm_2

            # mark the first algorithm as production-ready
            status_1 = MLAlgorithmStatus(status="production",
                                         created_by=ab_test.created_by,
                                         parent_mlalgorithm=alg_id_1,
                                         active=True)
            status_1.save()
            deactivate_other_statuses(status_1)

            # update status for second algorithm
            status_2 = MLAlgorithmStatus(status="testing",
                                         created_by=ab_test.created_by,
                                         parent_mlalgorithm=alg_id_2,
                                         active=True)
            status_2.save()
            deactivate_other_statuses(status_2)

            summary = "Algorithm #1 output: {}, Algorithm #2 output: {}".format(all_responses_1, all_responses_2)
            ab_test.ended_at = date_now
            ab_test.summary = summary
            ab_test.save()

        except Exception as e:
            return Response({"status": "Error", "message": str(e)},
                            status=status.HTTP_400_BAD_REQUEST
                            )
        return Response({"message": "AB Test finished.", "summary": summary})
