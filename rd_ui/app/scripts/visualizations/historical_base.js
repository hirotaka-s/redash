(function () {
  var HistoricalVisualizationProvider = function () {
    this.visualizations = {};
    this.visualizationTypes = {};
    var defaultConfig = {
      defaultOptions: {},
      skipTypes: false,
      editorTemplate: null
    };

    this.registerVisualization = function (config) {
      var visualization = _.extend({}, defaultConfig, config);

      // TODO: this is prone to errors; better refactor.
      if (_.isEmpty(this.visualizations)) {
        this.defaultVisualization = visualization;
      }

      this.visualizations[config.type] = visualization;

      if (!config.skipTypes) {
        this.visualizationTypes[config.name] = config.type;
      }
    };

    this.getSwitchTemplate = function (property) {
      var pattern = /(<[a-zA-Z0-9-]*?)( |>)/;

      var mergedTemplates = _.reduce(this.visualizations, function (templates, visualization) {
        if (visualization[property]) {
          var ngSwitch = '$1 ng-switch-when="' + visualization.type + '" $2';
          var template = visualization[property].replace(pattern, ngSwitch);

          return templates + "\n" + template;
        }

        return templates;
      }, "");

      mergedTemplates = '<div ng-switch on="visualization.type">' + mergedTemplates + "</div>";

      return mergedTemplates;
    };

    this.$get = ['$resource', function ($resource) {
      var HistoricalVisualization = $resource('api/visualizations/:id', {id: '@id'});
      HistoricalVisualization.visualizations = this.visualizations;
      HistoricalVisualization.visualizationTypes = this.visualizationTypes;
      HistoricalVisualization.renderVisualizationsTemplate = this.getSwitchTemplate('renderTemplate');
      HistoricalVisualization.editorTemplate = this.getSwitchTemplate('editorTemplate');
      HistoricalVisualization.defaultVisualization = this.defaultVisualization;

      return HistoricalVisualization;
    }];
  };

  var HistoricalVisualizationName = function(HistoricalVisualization) {
    return {
      restrict: 'E',
      scope: {
        visualization: '='
      },
      template: '{{name}}',
      replace: false,
      link: function (scope) {
        if (HistoricalVisualization.visualizations[scope.visualization.type].name !== scope.visualization.name) {
          scope.name = scope.visualization.name;
        }
      }
    };
  };

  var HistoricalVisualizationRenderer = function ($location, HistoricalVisualization) {
    return {
      restrict: 'E',
      scope: {
        visualization: '=',
        historicalQueryResult: '='
      },
      // TODO: using switch here (and in the options editor) might introduce errors and bad
      // performance wise. It's better to eventually show the correct template based on the
      // visualization type and not make the browser render all of them.
      template: '<filters></filters>\n' + HistoricalVisualization.renderVisualizationsTemplate,
      replace: false,
      link: function (scope) {
        scope.$watch('historicalQueryResult && historicalQueryResult.getFilters()', function (filters) {
          if (filters) {
            scope.filters = filters;
          }
        });
      }
    };
  };

  var HistoricalVisualizationOptionsEditor = function (HistoricalVisualization) {
    return {
      restrict: 'E',
      template: HistoricalVisualization.editorTemplate,
      replace: false
    };
  };

  var Filters = function () {
    return {
      restrict: 'E',
      templateUrl: '/views/visualizations/filters.html'
    };
  };

  var FilterValueFilter = function() {
    return function(value, filter) {
      if (_.isArray(value)) {
        value = value[0];
      }

      // TODO: deduplicate code with table.js:
      if (filter.column.type === 'date') {
        if (value && moment.isMoment(value)) {
          return value.format(clientConfig.dateFormat);
        }
      } else if (filter.column.type === 'datetime') {
        if (value && moment.isMoment(value)) {
          return value.format(clientConfig.dateTimeFormat);
        }
      }

      return value;
    };
  };

  var HistoricalEditVisualizationForm = function (Events, HistoricalVisualization, growl) {
    return {
      restrict: 'E',
      templateUrl: '/views/visualizations/historical_edit_visualization.html',
      replace: true,
      scope: {
        query: '=',
        historicalQueryResult: '=',
        originalVisualization: '=?',
        onNewSuccess: '=?',
        modalInstance: '=?'
      },
      link: function (scope) {
        scope.visualization = angular.copy(scope.originalVisualization);
        scope.editRawOptions = currentUser.hasPermission('edit_raw_chart');
        scope.visTypes = HistoricalVisualization.visualizationTypes;

        scope.newVisualization = function () {
          return {
            'type': HistoricalVisualization.defaultVisualization.type,
            'name': HistoricalVisualization.defaultVisualization.name,
            'description': '',
            'options': HistoricalVisualization.defaultVisualization.defaultOptions
          };
        };

        if (!scope.visualization) {
          var unwatch = scope.$watch('query.id', function (queryId) {
            if (queryId) {
              unwatch();

              scope.visualization = scope.newVisualization();
            }
          });
        }

        scope.$watch('visualization.type', function (type, oldType) {
          // if not edited by user, set name to match type
          if (type && oldType !== type && scope.visualization && !scope.visForm.name.$dirty) {
            scope.visualization.name = HistoricalVisualization.visualizations[scope.visualization.type].name;
          }

          if (type && oldType !== type && scope.visualization) {
            scope.visualization.options = HistoricalVisualization.visualizations[scope.visualization.type].defaultOptions;
          }
        });

        scope.submit = function () {
          if (scope.visualization.id) {
            Events.record(currentUser, "update", "visualization", scope.visualization.id, {'type': scope.visualization.type});
          } else {
            Events.record(currentUser, "create", "visualization", null, {'type': scope.visualization.type});
          }

          scope.visualization.query_id = scope.query.id;

          HistoricalVisualization.save(scope.visualization, function success(result) {
            growl.addSuccessMessage("HistoricalVisualization saved");

            var visIds = _.pluck(scope.query.visualizations, 'id');
            var index = visIds.indexOf(result.id);
            if (index > -1) {
              scope.query.visualizations[index] = result;
            } else {
              // new visualization
              scope.query.visualizations.push(result);
              scope.onNewSuccess && scope.onNewSuccess(result);
            }
            scope.modalInstance.close();
          }, function error() {
            growl.addErrorMessage("HistoricalVisualization could not be saved");
          });
        };

        scope.close = function() {
          if (scope.visForm.$dirty) {
            if (confirm("Are you sure you want to close the editor without saving?")) {
              scope.modalInstance.close();
            }
          } else {
            scope.modalInstance.close();
          }
        }
      }
    };
  };

  angular.module('redash.historical_visualization', [])
      .provider('HistoricalVisualization', HistoricalVisualizationProvider)
      .directive('historicalVisualizationRenderer', ['$location', 'HistoricalVisualization', HistoricalVisualizationRenderer])
      .directive('historicalVisualizationOptionsEditor', ['HistoricalVisualization', HistoricalVisualizationOptionsEditor])
      .directive('historicalVisualizationName', ['HistoricalVisualization', HistoricalVisualizationName])
      .directive('historicalEditVisulatizationForm', ['Events', 'HistoricalVisualization', 'growl', HistoricalEditVisualizationForm]);
})();
